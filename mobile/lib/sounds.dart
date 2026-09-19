import 'dart:math';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';

import 'store.dart';

/// Small synthesised cues: wake, sleep, a tick when a task starts, done, error.
class Sounds {
  static const _rate = 22050;

  // [from Hz, to Hz, delay s, length s, volume]
  static const _cues = {
    'wake': [
      [620.0, 980.0, 0.0, 0.28, 0.5]
    ],
    'sleep': [
      [900.0, 520.0, 0.0, 0.28, 0.5]
    ],
    'tick': [
      [1400.0, 1400.0, 0.0, 0.05, 0.25]
    ],
    'done': [
      [660.0, 660.0, 0.0, 0.14, 0.4],
      [990.0, 990.0, 0.1, 0.22, 0.4]
    ],
    'error': [
      [330.0, 250.0, 0.0, 0.3, 0.45]
    ],
  };

  static AudioPlayer? _player;
  static final _cache = <String, Uint8List>{};
  static String _lastKind = '';
  static DateTime _lastAt = DateTime.fromMillisecondsSinceEpoch(0);

  static Future<void> play(String kind) async {
    if (!Store.instance.sounds || !_cues.containsKey(kind)) return;
    final now = DateTime.now();
    final gap = kind == 'tick' ? 1200 : 2500;
    if (kind == _lastKind && now.difference(_lastAt).inMilliseconds < gap) return;
    _lastKind = kind;
    _lastAt = now;
    try {
      final player = _player ??= AudioPlayer()
        ..setAudioContext(AudioContextConfig(
          route: AudioContextConfigRoute.speaker,
          focus: AudioContextConfigFocus.mixWithOthers,
        ).build());
      await player.stop();
      await player.play(BytesSource(_cache[kind] ??= _render(_cues[kind]!), mimeType: 'audio/wav'));
    } catch (_) {
      // a missing cue is never worth an error
    }
  }

  static Uint8List _render(List<List<double>> notes) {
    final seconds = notes.map((n) => n[2] + n[3]).reduce(max) + 0.03;
    final samples = Float64List((seconds * _rate).ceil());
    for (final [from, to, delay, length, volume] in notes) {
      final start = (delay * _rate).round();
      final count = (length * _rate).round();
      var phase = 0.0;
      for (var i = 0; i < count && start + i < samples.length; i++) {
        final t = i / _rate;
        final sweep = (t / (length * 0.65)).clamp(0.0, 1.0);
        final freq = from * pow(to / from, sweep);
        phase += 2 * pi * freq / _rate;
        final attack = min(1.0, t / 0.015);
        final decay = exp(-5 * t / length);
        samples[start + i] += sin(phase) * volume * attack * decay;
      }
    }
    final pcm = ByteData(samples.length * 2);
    for (var i = 0; i < samples.length; i++) {
      pcm.setInt16(i * 2, (samples[i].clamp(-1.0, 1.0) * 32767).round(), Endian.little);
    }
    final header = ByteData(44);
    void ascii(int at, String s) {
      for (var i = 0; i < s.length; i++) {
        header.setUint8(at + i, s.codeUnitAt(i));
      }
    }

    final dataLength = pcm.lengthInBytes;
    ascii(0, 'RIFF');
    header.setUint32(4, 36 + dataLength, Endian.little);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    header.setUint32(16, 16, Endian.little);
    header.setUint16(20, 1, Endian.little);
    header.setUint16(22, 1, Endian.little);
    header.setUint32(24, _rate, Endian.little);
    header.setUint32(28, _rate * 2, Endian.little);
    header.setUint16(32, 2, Endian.little);
    header.setUint16(34, 16, Endian.little);
    ascii(36, 'data');
    header.setUint32(40, dataLength, Endian.little);
    return Uint8List.fromList([...header.buffer.asUint8List(), ...pcm.buffer.asUint8List()]);
  }
}
