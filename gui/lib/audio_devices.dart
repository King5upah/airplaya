// The list of audio output devices, as the receiver sees them.
//
// The receiver is the authority here: it is the process that will open the
// device, and it enumerates through PortAudio. Asking it (`--list-audio-devices
// --json`) avoids a second, possibly disagreeing, list on the Dart side.

import 'dart:convert';
import 'dart:io';

class AudioDevice {
  AudioDevice({
    required this.index,
    required this.name,
    required this.hostApi,
    required this.channels,
  });

  factory AudioDevice.fromJson(Map<String, dynamic> json) => AudioDevice(
    index: json['index'] as int,
    name: json['name'] as String,
    hostApi: (json['hostApi'] as String?) ?? '',
    channels: (json['channels'] as int?) ?? 2,
  );

  final int index;
  final String name;
  final String hostApi;
  final int channels;

  String get label => hostApi.isEmpty ? name : '$name  ·  $hostApi';
}

class AudioDeviceService {
  /// Ask the receiver for its device list.
  ///
  /// Returns an empty list when PortAudio is missing or Python cannot be run;
  /// the caller then offers the system default only, which still works.
  static Future<List<AudioDevice>> list() async {
    try {
      final result = await Process.run('python', [
        '-m',
        'airplaya',
        '--list-audio-devices',
        '--json',
      ]);
      if (result.exitCode != 0) return [];
      final decoded = jsonDecode('${result.stdout}'.trim());
      if (decoded is! List) return [];
      final devices = decoded
          .whereType<Map<String, dynamic>>()
          .map(AudioDevice.fromJson)
          .toList();
      // Prefer the modern host APIs: WASAPI entries name the real endpoint,
      // while the legacy MME list truncates names at 31 characters.
      const priority = ['Windows WASAPI', 'Windows DirectSound', 'MME'];
      devices.sort((a, b) {
        final rankA = priority.indexOf(a.hostApi);
        final rankB = priority.indexOf(b.hostApi);
        final byApi = (rankA < 0 ? priority.length : rankA).compareTo(
          rankB < 0 ? priority.length : rankB,
        );
        return byApi != 0 ? byApi : a.name.compareTo(b.name);
      });
      return devices;
    } on ProcessException {
      return [];
    } on FormatException {
      return [];
    }
  }
}
