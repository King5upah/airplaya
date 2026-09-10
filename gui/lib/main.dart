// airplaya — desktop front end for the AirPlay receiver.
//
// The receiver itself is the Python package in this repository. This app is a
// control panel for it: it starts `python -m airplaya`, follows the log the
// receiver writes to stderr, and turns that into a status the user can read at
// a glance. The mirrored video is decoded by ffplay in its own window, so no
// video pipeline is needed here.

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'audio_devices.dart';
import 'setup.dart';

void main() {
  runApp(const AirplayaApp());
}

// ---------------------------------------------------------------------------
// Receiver process
// ---------------------------------------------------------------------------

/// What the receiver is doing, inferred from its log output.
enum ReceiverState { stopped, starting, advertising, mirroring, failed }

class LogLine {
  LogLine(this.raw) : level = _levelOf(raw);

  final String raw;
  final String level;

  static String _levelOf(String line) {
    for (final level in const ['ERROR', 'WARNI', 'DEBUG', 'TRACE', 'INFO']) {
      if (line.contains(' $level')) return level.trim();
    }
    return 'INFO';
  }
}

/// Owns the child process and derives state from its output.
class ReceiverController extends ChangeNotifier {
  static const _maxLines = 500;

  Process? _process;
  final List<LogLine> _log = [];
  ReceiverState _state = ReceiverState.stopped;
  String? _address;
  String? _clientName;
  String? _error;

  List<LogLine> get log => List.unmodifiable(_log);
  ReceiverState get state => _state;
  String? get address => _address;
  String? get clientName => _clientName;
  String? get error => _error;
  bool get isRunning => _process != null;

  Future<void> start({
    required String name,
    required String sink,
    required bool verbose,
    int? audioDeviceIndex,
    bool audioEnabled = true,
  }) async {
    if (_process != null) return;

    _log.clear();
    _error = null;
    _address = null;
    _clientName = null;
    _setState(ReceiverState.starting);

    final arguments = <String>[
      '-u', // unbuffered, or the log arrives in unhelpful bursts
      '-m',
      'airplaya',
      '--name',
      name,
      '--sink',
      sink,
      if (!audioEnabled) '--no-audio',
      if (audioEnabled && audioDeviceIndex != null) ...[
        '--audio-device',
        '$audioDeviceIndex',
      ],
      if (verbose) '-v',
    ];

    try {
      final process = await Process.start('python', arguments);
      _process = process;

      process.stderr
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .listen(_onLogLine);
      process.stdout
          .transform(utf8.decoder)
          .transform(const LineSplitter())
          .listen(_onLogLine);

      unawaited(process.exitCode.then(_onExit));
    } on ProcessException catch (exception) {
      _process = null;
      _error =
          'Could not run python: ${exception.message}\n'
          'Install the receiver with: pip install -e .';
      _setState(ReceiverState.failed);
    }
  }

  Future<void> stop() async {
    final process = _process;
    if (process == null) return;
    // The receiver shuts down cleanly on SIGINT, which unregisters its mDNS
    // record; killing it outright leaves a stale entry in the iOS picker.
    if (!process.kill(ProcessSignal.sigint)) {
      process.kill();
    }
    await process.exitCode.timeout(
      const Duration(seconds: 4),
      onTimeout: () {
        process.kill();
        return -1;
      },
    );
  }

  void _onLogLine(String line) {
    if (line.trim().isEmpty) return;

    _log.add(LogLine(line));
    if (_log.length > _maxLines) {
      _log.removeRange(0, _log.length - _maxLines);
    }

    // The receiver's own log is the only status channel, so parse it.
    if (line.contains('mirror client connected')) {
      _state = ReceiverState.mirroring;
    } else if (line.contains('mirror client disconnected')) {
      _state = ReceiverState.advertising;
      _clientName = null;
    } else if (line.contains('ready.')) {
      _state = ReceiverState.advertising;
      _address = RegExp(r'at (\d+\.\d+\.\d+\.\d+)').firstMatch(line)?.group(1);
    } else if (line.contains('client: ')) {
      _clientName = RegExp(r'client: ([^(]+)').firstMatch(line)?.group(1)?.trim();
    }

    notifyListeners();
  }

  void _onExit(int code) {
    _process = null;
    if (code != 0 && _state != ReceiverState.stopped) {
      _error = 'The receiver exited with code $code. See the log below.';
      _setState(ReceiverState.failed);
    } else {
      _setState(ReceiverState.stopped);
    }
  }

  void _setState(ReceiverState state) {
    _state = state;
    notifyListeners();
  }

  @override
  void dispose() {
    _process?.kill();
    super.dispose();
  }
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

const _ink = Color(0xFF0E1116);
const _panel = Color(0xFF161B22);
const _line = Color(0xFF262C36);
const _text = Color(0xFFE6EDF3);
const _muted = Color(0xFF8B949E);
const _accent = Color(0xFF4CC2FF);
const _good = Color(0xFF3FB950);
const _warn = Color(0xFFD29922);
const _bad = Color(0xFFF85149);

class AirplayaApp extends StatelessWidget {
  const AirplayaApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'airplaya',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        scaffoldBackgroundColor: _ink,
        colorScheme: const ColorScheme.dark(
          primary: _accent,
          surface: _panel,
          onSurface: _text,
        ),
        fontFamily: 'Segoe UI',
      ),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final _controller = ReceiverController();
  final _nameController = TextEditingController(text: 'airplaya');
  final _scrollController = ScrollController();
  String _sink = 'ffplay';
  bool _verbose = true;
  List<Check> _checks = [];
  bool _repairing = false;
  String? _repairMessage;
  List<AudioDevice> _audioDevices = [];
  int? _audioDeviceIndex; // null means the system default
  bool _audioEnabled = true;

  @override
  void initState() {
    super.initState();
    _controller.addListener(_onUpdate);
    _runChecks();
    _loadAudioDevices();
  }

  Future<void> _loadAudioDevices() async {
    final devices = await AudioDeviceService.list();
    if (!mounted) return;
    setState(() {
      _audioDevices = devices;
      // Drop a saved selection that no longer exists — devices come and go
      // with headsets and monitors.
      if (!devices.any((d) => d.index == _audioDeviceIndex)) {
        _audioDeviceIndex = null;
      }
    });
  }

  Future<void> _runChecks() async {
    final checks = await SetupService.inspect();
    if (mounted) setState(() => _checks = checks);
  }

  Future<void> _repair() async {
    setState(() {
      _repairing = true;
      _repairMessage = null;
    });
    final failure = await SetupService.repairFirewall();
    await _runChecks();
    if (!mounted) return;
    setState(() {
      _repairing = false;
      _repairMessage = failure;
    });
  }

  void _onUpdate() {
    setState(() {});
    // Keep the newest line in view unless the user has scrolled up.
    if (_scrollController.hasClients) {
      final position = _scrollController.position;
      if (position.pixels > position.maxScrollExtent - 120) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (_scrollController.hasClients) {
            _scrollController.jumpTo(_scrollController.position.maxScrollExtent);
          }
        });
      }
    }
  }

  @override
  void dispose() {
    _controller.removeListener(_onUpdate);
    _controller.dispose();
    _nameController.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  Future<void> _toggle() async {
    if (_controller.isRunning) {
      await _controller.stop();
    } else {
      await _controller.start(
        name: _nameController.text.trim().isEmpty
            ? 'airplaya'
            : _nameController.text.trim(),
        sink: _sink,
        verbose: _verbose,
        audioDeviceIndex: _audioDeviceIndex,
        audioEnabled: _audioEnabled,
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _Header(state: _controller.state),
            const SizedBox(height: 18),
            _ControlsPanel(
              nameController: _nameController,
              sink: _sink,
              verbose: _verbose,
              running: _controller.isRunning,
              onSinkChanged: (value) => setState(() => _sink = value),
              onVerboseChanged: (value) => setState(() => _verbose = value),
              onToggle: _toggle,
            ),
            const SizedBox(height: 14),
            _AudioPanel(
              devices: _audioDevices,
              selected: _audioDeviceIndex,
              enabled: _audioEnabled,
              running: _controller.isRunning,
              onDeviceChanged: (index) => setState(() => _audioDeviceIndex = index),
              onEnabledChanged: (value) => setState(() => _audioEnabled = value),
              onRefresh: _loadAudioDevices,
            ),
            const SizedBox(height: 14),
            _StatusStrip(
              state: _controller.state,
              address: _controller.address,
              clientName: _controller.clientName,
              name: _nameController.text,
            ),
            if (_controller.error != null) ...[
              const SizedBox(height: 14),
              _ErrorBanner(message: _controller.error!),
            ],
            if (_checks.any((c) => c.status != CheckStatus.pass)) ...[
              const SizedBox(height: 14),
              _PreflightPanel(
                checks: _checks,
                repairing: _repairing,
                message: _repairMessage,
                onRepair: _repair,
                onRecheck: _runChecks,
              ),
            ],
            const SizedBox(height: 14),
            Expanded(
              child: _LogPanel(
                lines: _controller.log,
                scrollController: _scrollController,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.state});

  final ReceiverState state;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 40,
          height: 40,
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(10),
            gradient: const LinearGradient(
              colors: [_accent, Color(0xFF7A5CFF)],
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
            ),
          ),
          child: const Icon(Icons.screen_share_outlined, color: Colors.white, size: 22),
        ),
        const SizedBox(width: 12),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'airplaya',
              style: TextStyle(
                color: _text,
                fontSize: 20,
                fontWeight: FontWeight.w600,
                height: 1.1,
              ),
            ),
            Text(
              'AirPlay mirroring receiver',
              style: TextStyle(color: _muted, fontSize: 12.5),
            ),
          ],
        ),
        const Spacer(),
        _StatePill(state: state),
      ],
    );
  }
}

class _StatePill extends StatelessWidget {
  const _StatePill({required this.state});

  final ReceiverState state;

  @override
  Widget build(BuildContext context) {
    final (label, color) = switch (state) {
      ReceiverState.stopped => ('Stopped', _muted),
      ReceiverState.starting => ('Starting', _warn),
      ReceiverState.advertising => ('Waiting for a device', _accent),
      ReceiverState.mirroring => ('Mirroring', _good),
      ReceiverState.failed => ('Failed', _bad),
    };

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: color.withValues(alpha: 0.4)),
      ),
      child: Row(
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(color: color, shape: BoxShape.circle),
          ),
          const SizedBox(width: 8),
          Text(
            label,
            style: TextStyle(color: color, fontSize: 12.5, fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }
}

class _ControlsPanel extends StatelessWidget {
  const _ControlsPanel({
    required this.nameController,
    required this.sink,
    required this.verbose,
    required this.running,
    required this.onSinkChanged,
    required this.onVerboseChanged,
    required this.onToggle,
  });

  final TextEditingController nameController;
  final String sink;
  final bool verbose;
  final bool running;
  final ValueChanged<String> onSinkChanged;
  final ValueChanged<bool> onVerboseChanged;
  final VoidCallback onToggle;

  @override
  Widget build(BuildContext context) {
    return _Panel(
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Expanded(
            flex: 3,
            child: _Field(
              label: 'Name shown on the iPhone',
              child: TextField(
                controller: nameController,
                enabled: !running,
                style: const TextStyle(color: _text, fontSize: 14),
                decoration: _inputDecoration(),
              ),
            ),
          ),
          const SizedBox(width: 14),
          Expanded(
            flex: 2,
            child: _Field(
              label: 'Video output',
              child: DropdownButtonFormField<String>(
                initialValue: sink,
                dropdownColor: _panel,
                style: const TextStyle(color: _text, fontSize: 14),
                decoration: _inputDecoration(),
                items: const [
                  DropdownMenuItem(value: 'ffplay', child: Text('ffplay window')),
                  DropdownMenuItem(value: 'file', child: Text('File (needs a path)')),
                  DropdownMenuItem(value: 'null', child: Text('Discard')),
                ],
                onChanged: running ? null : (value) => onSinkChanged(value ?? 'ffplay'),
              ),
            ),
          ),
          const SizedBox(width: 14),
          _Field(
            label: 'Protocol log',
            child: SizedBox(
              height: 44,
              child: Row(
                children: [
                  Switch(
                    value: verbose,
                    activeThumbColor: _accent,
                    onChanged: running ? null : onVerboseChanged,
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(width: 14),
          SizedBox(
            height: 44,
            child: FilledButton.icon(
              onPressed: onToggle,
              icon: Icon(running ? Icons.stop_rounded : Icons.play_arrow_rounded),
              label: Text(running ? 'Stop' : 'Start receiver'),
              style: FilledButton.styleFrom(
                backgroundColor: running ? _bad : _accent,
                foregroundColor: running ? Colors.white : _ink,
                padding: const EdgeInsets.symmetric(horizontal: 20),
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
              ),
            ),
          ),
        ],
      ),
    );
  }

  InputDecoration _inputDecoration() {
    const border = OutlineInputBorder(
      borderSide: BorderSide(color: _line),
      borderRadius: BorderRadius.all(Radius.circular(8)),
    );
    return const InputDecoration(
      isDense: true,
      filled: true,
      fillColor: _ink,
      contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 13),
      border: border,
      enabledBorder: border,
      focusedBorder: OutlineInputBorder(
        borderSide: BorderSide(color: _accent),
        borderRadius: BorderRadius.all(Radius.circular(8)),
      ),
    );
  }
}

/// Audio output choice. Locked while the receiver runs, because the device is
/// opened at stream start.
class _AudioPanel extends StatelessWidget {
  const _AudioPanel({
    required this.devices,
    required this.selected,
    required this.enabled,
    required this.running,
    required this.onDeviceChanged,
    required this.onEnabledChanged,
    required this.onRefresh,
  });

  final List<AudioDevice> devices;
  final int? selected;
  final bool enabled;
  final bool running;
  final ValueChanged<int?> onDeviceChanged;
  final ValueChanged<bool> onEnabledChanged;
  final VoidCallback onRefresh;

  @override
  Widget build(BuildContext context) {
    final locked = running || !enabled;
    return _Panel(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          _Field(
            label: 'Audio',
            child: SizedBox(
              height: 44,
              child: Row(
                children: [
                  Switch(
                    value: enabled,
                    activeThumbColor: _accent,
                    onChanged: running ? null : onEnabledChanged,
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(width: 6),
          Expanded(
            child: _Field(
              label: 'Output device',
              child: DropdownButtonFormField<int?>(
                initialValue: selected,
                isExpanded: true,
                dropdownColor: _panel,
                style: const TextStyle(color: _text, fontSize: 13.5),
                decoration: const InputDecoration(
                  isDense: true,
                  filled: true,
                  fillColor: _ink,
                  contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 13),
                  border: OutlineInputBorder(
                    borderSide: BorderSide(color: _line),
                    borderRadius: BorderRadius.all(Radius.circular(8)),
                  ),
                  enabledBorder: OutlineInputBorder(
                    borderSide: BorderSide(color: _line),
                    borderRadius: BorderRadius.all(Radius.circular(8)),
                  ),
                ),
                items: [
                  const DropdownMenuItem<int?>(
                    value: null,
                    child: Text('System default'),
                  ),
                  for (final device in devices)
                    DropdownMenuItem<int?>(
                      value: device.index,
                      child: Text(device.label, overflow: TextOverflow.ellipsis),
                    ),
                ],
                onChanged: locked ? null : onDeviceChanged,
              ),
            ),
          ),
          const SizedBox(width: 8),
          IconButton(
            tooltip: 'Refresh the device list',
            onPressed: running ? null : onRefresh,
            icon: const Icon(Icons.refresh, size: 18, color: _muted),
          ),
        ],
      ),
    );
  }
}

class _StatusStrip extends StatelessWidget {
  const _StatusStrip({
    required this.state,
    required this.address,
    required this.clientName,
    required this.name,
  });

  final ReceiverState state;
  final String? address;
  final String? clientName;
  final String name;

  @override
  Widget build(BuildContext context) {
    final hint = switch (state) {
      ReceiverState.stopped => 'Press Start, then pick the receiver on your iPhone.',
      ReceiverState.starting => 'Binding ports and registering over Bonjour…',
      ReceiverState.advertising =>
        'On the iPhone: Control Centre → Screen Mirroring → "$name".',
      ReceiverState.mirroring => clientName == null
          ? 'A device is mirroring. The video is in the ffplay window.'
          : '$clientName is mirroring. The video is in the ffplay window.',
      ReceiverState.failed => 'The receiver stopped. The log has the reason.',
    };

    return _Panel(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 13),
      child: Row(
        children: [
          Icon(
            state == ReceiverState.mirroring
                ? Icons.check_circle_outline
                : Icons.info_outline,
            size: 17,
            color: state == ReceiverState.mirroring ? _good : _muted,
          ),
          const SizedBox(width: 10),
          Expanded(child: Text(hint, style: const TextStyle(color: _text, fontSize: 13))),
          if (address != null) ...[
            const SizedBox(width: 12),
            _Mono(text: address!),
          ],
        ],
      ),
    );
  }
}

class _ErrorBanner extends StatelessWidget {
  const _ErrorBanner({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 13),
      decoration: BoxDecoration(
        color: _bad.withValues(alpha: 0.10),
        border: Border.all(color: _bad.withValues(alpha: 0.35)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.error_outline, color: _bad, size: 18),
          const SizedBox(width: 10),
          Expanded(
            child: SelectableText(
              message,
              style: const TextStyle(color: _text, fontSize: 13, height: 1.4),
            ),
          ),
        ],
      ),
    );
  }
}

/// Shows only the checks that are not passing, plus the one-click repair.
class _PreflightPanel extends StatelessWidget {
  const _PreflightPanel({
    required this.checks,
    required this.repairing,
    required this.message,
    required this.onRepair,
    required this.onRecheck,
  });

  final List<Check> checks;
  final bool repairing;
  final String? message;
  final VoidCallback onRepair;
  final VoidCallback onRecheck;

  @override
  Widget build(BuildContext context) {
    final problems = checks.where((c) => c.status != CheckStatus.pass).toList();
    final repairable = problems.any((c) => c.repairable);

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      decoration: BoxDecoration(
        color: _warn.withValues(alpha: 0.08),
        border: Border.all(color: _warn.withValues(alpha: 0.35)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.build_outlined, color: _warn, size: 17),
              const SizedBox(width: 9),
              const Text(
                'Setup needed',
                style: TextStyle(color: _text, fontSize: 13.5, fontWeight: FontWeight.w600),
              ),
              const Spacer(),
              TextButton(
                onPressed: repairing ? null : onRecheck,
                child: const Text('Re-check', style: TextStyle(fontSize: 12.5)),
              ),
              if (repairable) ...[
                const SizedBox(width: 6),
                FilledButton.icon(
                  onPressed: repairing ? null : onRepair,
                  icon: repairing
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.shield_outlined, size: 16),
                  label: Text(repairing ? 'Working…' : 'Fix it (needs admin)'),
                  style: FilledButton.styleFrom(
                    backgroundColor: _warn,
                    foregroundColor: _ink,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                  ),
                ),
              ],
            ],
          ),
          const SizedBox(height: 10),
          for (final problem in problems)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(
                    problem.status == CheckStatus.fail
                        ? Icons.close_rounded
                        : Icons.help_outline,
                    size: 15,
                    color: problem.status == CheckStatus.fail ? _bad : _muted,
                  ),
                  const SizedBox(width: 9),
                  Expanded(
                    child: Text.rich(
                      TextSpan(
                        children: [
                          TextSpan(
                            text: '${problem.label}. ',
                            style: const TextStyle(color: _text, fontSize: 12.5),
                          ),
                          TextSpan(
                            text: problem.detail,
                            style: const TextStyle(color: _muted, fontSize: 12.5),
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
            ),
          if (message != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(
                message!,
                style: const TextStyle(color: _bad, fontSize: 12.5),
              ),
            ),
        ],
      ),
    );
  }
}

class _LogPanel extends StatelessWidget {
  const _LogPanel({required this.lines, required this.scrollController});

  final List<LogLine> lines;
  final ScrollController scrollController;

  static const _levelColors = {
    'ERROR': _bad,
    'WARNI': _warn,
    'DEBUG': _muted,
    'TRACE': Color(0xFF6E7681),
    'INFO': _text,
  };

  @override
  Widget build(BuildContext context) {
    return _Panel(
      padding: EdgeInsets.zero,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            decoration: const BoxDecoration(
              border: Border(bottom: BorderSide(color: _line)),
            ),
            child: Row(
              children: [
                const Text(
                  'Receiver log',
                  style: TextStyle(color: _muted, fontSize: 12, fontWeight: FontWeight.w600),
                ),
                const Spacer(),
                if (lines.isNotEmpty)
                  IconButton(
                    tooltip: 'Copy the log',
                    icon: const Icon(Icons.copy_all_outlined, size: 16, color: _muted),
                    onPressed: () => Clipboard.setData(
                      ClipboardData(text: lines.map((l) => l.raw).join('\n')),
                    ),
                  ),
              ],
            ),
          ),
          Expanded(
            child: lines.isEmpty
                ? const Center(
                    child: Text(
                      'Nothing yet.',
                      style: TextStyle(color: _muted, fontSize: 13),
                    ),
                  )
                : ListView.builder(
                    controller: scrollController,
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                    itemCount: lines.length,
                    itemBuilder: (context, index) {
                      final line = lines[index];
                      return Padding(
                        padding: const EdgeInsets.symmetric(vertical: 1.5),
                        child: Text(
                          line.raw,
                          style: TextStyle(
                            color: _levelColors[line.level] ?? _text,
                            fontFamily: 'Consolas',
                            fontSize: 12,
                            height: 1.35,
                          ),
                        ),
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}

// -- small shared pieces ----------------------------------------------------

class _Panel extends StatelessWidget {
  const _Panel({required this.child, this.padding});

  final Widget child;
  final EdgeInsets? padding;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: padding ?? const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _line),
      ),
      child: child,
    );
  }
}

class _Field extends StatelessWidget {
  const _Field({required this.label, required this.child});

  final String label;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: const TextStyle(color: _muted, fontSize: 11.5, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 6),
        child,
      ],
    );
  }
}

class _Mono extends StatelessWidget {
  const _Mono({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
      decoration: BoxDecoration(
        color: _ink,
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: _line),
      ),
      child: Text(
        text,
        style: const TextStyle(color: _accent, fontFamily: 'Consolas', fontSize: 12),
      ),
    );
  }
}
