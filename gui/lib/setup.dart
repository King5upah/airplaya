// Preflight checks and one-click repair.
//
// A mirroring receiver needs more than its own process to work: Python has to
// be able to import the package, ffplay has to exist, and Windows Firewall has
// to let the iPhone open a TCP connection inbound. That last one is the usual
// reason the phone shows "unable to connect" — the receiver is advertising
// fine over mDNS, so it appears in the picker, and then the connection is
// dropped before it reaches us.
//
// The firewall rules are written for every profile rather than just the
// private one. A phone on the same Wi-Fi reaches us the same way whether
// Windows has decided the network is "public" or not, and re-categorising
// someone's network is a bigger change than opening two ports for one app.

import 'dart:convert';
import 'dart:io';

enum CheckStatus { pass, fail, unknown }

class Check {
  Check({
    required this.id,
    required this.label,
    required this.status,
    required this.detail,
    this.repairable = false,
  });

  final String id;
  final String label;
  final CheckStatus status;
  final String detail;
  final bool repairable;
}

/// The firewall rules the receiver needs, and the ports they cover.
const _tcpPorts = '7000,7100';
const _udpPorts = '6000,6001,7010';
const _tcpRuleName = 'airplaya (AirPlay control and video)';
const _udpRuleName = 'airplaya (AirPlay audio and timing)';

class SetupService {
  /// Run every check. Cheap enough to call on startup and after a repair.
  static Future<List<Check>> inspect() async {
    final results = await Future.wait([
      _checkPython(),
      _checkFfplay(),
      _checkFirewall(),
    ]);
    return results;
  }

  static Future<Check> _checkPython() async {
    try {
      final result = await Process.run('python', [
        '-c',
        'import airplaya, sys; print(airplaya.__version__)',
      ]);
      if (result.exitCode == 0) {
        return Check(
          id: 'python',
          label: 'Receiver installed',
          status: CheckStatus.pass,
          detail: 'airplaya ${(result.stdout as String).trim()}',
        );
      }
      return Check(
        id: 'python',
        label: 'Receiver installed',
        status: CheckStatus.fail,
        detail: 'Python cannot import airplaya. Run: pip install -e .',
      );
    } on ProcessException {
      return Check(
        id: 'python',
        label: 'Receiver installed',
        status: CheckStatus.fail,
        detail: 'python is not on PATH',
      );
    }
  }

  static Future<Check> _checkFfplay() async {
    try {
      final result = await Process.run('ffplay', ['-version']);
      final firstLine = (result.stdout as String).split('\n').first.trim();
      return Check(
        id: 'ffplay',
        label: 'Video player available',
        status: CheckStatus.pass,
        detail: firstLine.isEmpty ? 'ffplay found' : firstLine,
      );
    } on ProcessException {
      return Check(
        id: 'ffplay',
        label: 'Video player available',
        status: CheckStatus.fail,
        detail: 'ffplay is not on PATH. Install ffmpeg, or choose another output.',
      );
    }
  }

  static Future<Check> _checkFirewall() async {
    try {
      // `netsh` exits non-zero and prints "No rules match" when the rule is
      // absent, which is exactly the signal we want.
      final result = await Process.run('netsh', [
        'advfirewall',
        'firewall',
        'show',
        'rule',
        'name=$_tcpRuleName',
      ]);
      final output = '${result.stdout}';
      final present = result.exitCode == 0 && output.contains('Allow');
      if (present) {
        return Check(
          id: 'firewall',
          label: 'Firewall allows incoming connections',
          status: CheckStatus.pass,
          detail: 'TCP $_tcpPorts and UDP $_udpPorts are allowed',
        );
      }
      return Check(
        id: 'firewall',
        label: 'Firewall allows incoming connections',
        status: CheckStatus.fail,
        detail:
            'Without this the iPhone lists the receiver but cannot connect. '
            'Repair adds allow rules for TCP $_tcpPorts and UDP $_udpPorts.',
        repairable: true,
      );
    } on ProcessException {
      return Check(
        id: 'firewall',
        label: 'Firewall allows incoming connections',
        status: CheckStatus.unknown,
        detail: 'Could not query Windows Firewall',
      );
    }
  }

  /// Add the firewall rules. Prompts for administrator rights once.
  ///
  /// Returns null on success, or a message explaining what went wrong —
  /// including the user declining the prompt, which is a normal outcome.
  static Future<String?> repairFirewall() async {
    final script = await _writeRepairScript();
    final marker = File('${script.parent.path}\\airplaya_repair.done');
    if (marker.existsSync()) marker.deleteSync();

    try {
      final result = await Process.run('powershell', [
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        // Launching a second, elevated PowerShell is what raises the UAC
        // prompt; -Wait lets us report the outcome rather than guess.
        "\$p = Start-Process powershell -Verb RunAs -Wait -PassThru "
            "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','${script.path}'; "
            "exit \$p.ExitCode",
      ]);

      if (!marker.existsSync()) {
        final stderr = '${result.stderr}'.trim();
        if (stderr.contains('canceled') || stderr.contains('cancelled')) {
          return 'The administrator prompt was declined, so nothing changed.';
        }
        return stderr.isEmpty
            ? 'The repair did not complete. Nothing was changed.'
            : 'The repair failed: $stderr';
      }
      return null;
    } on ProcessException catch (exception) {
      return 'Could not start PowerShell: ${exception.message}';
    }
  }

  static Future<File> _writeRepairScript() async {
    final directory = Directory.systemTemp;
    final script = File('${directory.path}\\airplaya_firewall.ps1');
    final pythonPath = await _resolveExecutable('python');

    final lines = <String>[
      '# Generated by airplaya. Adds inbound firewall rules for the receiver.',
      '# Remove them with:',
      "#   netsh advfirewall firewall delete rule name='$_tcpRuleName'",
      "#   netsh advfirewall firewall delete rule name='$_udpRuleName'",
      '\$ErrorActionPreference = "Continue"',
      "netsh advfirewall firewall delete rule name='$_tcpRuleName' | Out-Null",
      "netsh advfirewall firewall add rule name='$_tcpRuleName' dir=in action=allow "
          'protocol=TCP localport=$_tcpPorts profile=any enable=yes',
      "netsh advfirewall firewall delete rule name='$_udpRuleName' | Out-Null",
      "netsh advfirewall firewall add rule name='$_udpRuleName' dir=in action=allow "
          'protocol=UDP localport=$_udpPorts profile=any enable=yes',
    ];

    if (pythonPath != null) {
      // A program rule covers the ephemeral sockets the receiver also opens.
      lines.addAll([
        "netsh advfirewall firewall delete rule name='airplaya receiver' | Out-Null",
        "netsh advfirewall firewall add rule name='airplaya receiver' dir=in "
            "action=allow program='$pythonPath' profile=any enable=yes",
      ]);
    }

    // The marker file is how the unelevated side knows the script ran to
    // completion rather than being cancelled at the UAC prompt.
    lines.add(
      "Set-Content -Path '${directory.path}\\airplaya_repair.done' -Value 'ok' -Encoding utf8",
    );

    await script.writeAsString(lines.join('\r\n'), encoding: utf8);
    return script;
  }

  static Future<String?> _resolveExecutable(String name) async {
    try {
      final result = await Process.run('where', [name]);
      if (result.exitCode != 0) return null;
      final first = '${result.stdout}'.split('\n').first.trim();
      return first.isEmpty ? null : first;
    } on ProcessException {
      return null;
    }
  }
}
