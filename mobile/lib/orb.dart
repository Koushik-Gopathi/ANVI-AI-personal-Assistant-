import 'dart:math';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

enum OrbMode { sleep, listening, thinking, speaking }

/// The glowing plexus orb: a rotating 3D point cloud whose nearby points are
/// joined by lines, with a bright core. It breathes with the voice level.
class Orb extends StatefulWidget {
  final OrbMode Function() mode;
  final double Function() level;
  const Orb({super.key, required this.mode, required this.level});

  @override
  State<Orb> createState() => _OrbState();
}

class _Node {
  final double x, y, z, r, ph, sp, amp, size;
  final bool dust, hot;
  _Node(this.x, this.y, this.z, this.r, this.ph, this.sp, this.amp, this.size, this.dust, this.hot);
}

class _OrbState extends State<Orb> with SingleTickerProviderStateMixin {
  late final Ticker _ticker;
  final _painter = _OrbPainter();
  Duration _last = Duration.zero;

  @override
  void initState() {
    super.initState();
    _ticker = createTicker((elapsed) {
      final dt = ((elapsed - _last).inMicroseconds / 1e6).clamp(0.0, 0.05);
      _last = elapsed;
      _painter.step(elapsed.inMicroseconds / 1e6, dt, widget.mode(), widget.level());
    })
      ..start();
  }

  @override
  void dispose() {
    _ticker.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => RepaintBoundary(child: CustomPaint(painter: _painter, size: Size.infinite));
}

class _OrbPainter extends CustomPainter {
  _OrbPainter() : super(repaint: _tick) {
    _build();
  }

  static final _tick = ValueNotifier<int>(0);
  static final _rnd = Random(3);
  final _nodes = <_Node>[];
  final _edges = <(int, int, double)>[];

  // animated look, eased toward per-mode targets
  double scale = 0.9, spin = 0.06, glow = 0.35, jitter = 0.015, line = 0.45, ripple = 0;
  double _level = 0, _rotY = 0, _t = 0;

  late Float64List _sx, _sy, _sd;

  double _rand(double a, double b) => a + _rnd.nextDouble() * (b - a);

  List<double> _dir() {
    while (true) {
      final x = _rand(-1, 1), y = _rand(-1, 1), z = _rand(-1, 1);
      final d = x * x + y * y + z * z;
      if (d > 1e-4 && d <= 1) {
        final s = sqrt(d);
        return [x / s, y / s, z / s];
      }
    }
  }

  double _lobe(double x, double y, double z) =>
      1 + 0.09 * sin(3 * x + 2 * y + 0.7) + 0.07 * cos(4 * z - 2 * x) + 0.05 * sin(5 * y + z);

  void _add(double rMin, double rMax, double pow_, String kind) {
    final d = _dir();
    final r = (rMin + pow(_rnd.nextDouble(), pow_) * (rMax - rMin)) * _lobe(d[0], d[1], d[2]);
    final dust = kind == 'dust';
    _nodes.add(_Node(d[0] * r, d[1] * r, d[2] * r, r, _rand(0, 2 * pi), _rand(0.6, 1.8), _rand(0.4, 1),
        dust ? _rand(0.6, 1.2) : _rand(0.9, 2.2), dust, _rnd.nextDouble() < 0.08));
  }

  void _build() {
    for (var i = 0; i < 300; i++) {
      _add(0.84, 1.0, 1, 'shell');
    }
    for (var i = 0; i < 280; i++) {
      _add(0.03, 0.84, 1.5, 'inner');
    }
    for (var i = 0; i < 110; i++) {
      _add(1.02, 1.35, 1.6, 'dust');
    }

    final linked = <int>{};
    void link(int i, int j, double w) {
      if (i == j) return;
      final key = i < j ? i * 4096 + j : j * 4096 + i;
      if (linked.add(key)) _edges.add((i, j, w));
    }

    final core = [for (var i = 0; i < _nodes.length; i++) if (!_nodes[i].dust) i];
    for (final i in core) {
      final a = _nodes[i];
      final near = <(double, int)>[];
      for (final j in core) {
        if (i == j) continue;
        final b = _nodes[j];
        final d = (a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y) + (a.z - b.z) * (a.z - b.z);
        if (d < 0.16) near.add((d, j));
      }
      near.sort((p, q) => p.$1.compareTo(q.$1));
      for (final n in near.take(a.r >= 0.84 ? 4 : 3)) {
        link(i, n.$2, 1);
      }
    }
    final hubs = core.where((i) => _nodes[i].r < 0.38).toList();
    final shell = core.where((i) => _nodes[i].r >= 0.84).toList();
    for (var s = 0; s < 260; s++) {
      link(shell[_rnd.nextInt(shell.length)], hubs[_rnd.nextInt(hubs.length)], 0.55);
    }
    for (var s = 0; s < 70; s++) {
      link(shell[_rnd.nextInt(shell.length)], shell[_rnd.nextInt(shell.length)], 0.35);
    }
    _sx = Float64List(_nodes.length);
    _sy = Float64List(_nodes.length);
    _sd = Float64List(_nodes.length);
  }

  void step(double t, double dt, OrbMode mode, double rawLevel) {
    _t = t;
    final raw = mode == OrbMode.listening
        ? min(1.0, rawLevel * 9)
        : mode == OrbMode.speaking
            ? 0.35 + 0.3 * sin(t * 9) * sin(t * 3.7).abs()
            : 0.0;
    _level += (raw - _level) * (raw > _level ? 0.45 : 0.08);
    final l = _level;

    final (tScale, tSpin, tGlow, tJitter, tLine, tRipple) = switch (mode) {
      OrbMode.listening => (0.94 + l * 0.35, 0.12, 0.55 + l * 1.4, 0.02 + l * 0.1, 0.62 + l * 0.6, l * 0.5),
      OrbMode.thinking => (0.86 + 0.035 * sin(t * 5), 0.85, 0.72 + 0.22 * sin(t * 6.5), 0.05, 0.78, 1.0),
      OrbMode.speaking => (1.0 + l * 0.3, 0.2, 0.7 + l * 1.6, 0.03 + l * 0.12, 0.7 + l * 0.7, l),
      OrbMode.sleep => (0.9, 0.06, 0.38, 0.015, 0.45, 0.0),
    };
    final k = min(1.0, dt * 5);
    scale += (tScale - scale) * k;
    spin += (tSpin - spin) * k;
    glow += (tGlow - glow) * k;
    jitter += (tJitter - jitter) * k;
    line += (tLine - line) * k;
    ripple += (tRipple - ripple) * k;
    _rotY += spin * dt;
    _tick.value++;
  }

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width, h = size.height;
    final R = min(w * 0.38, h * 0.26);
    final cx = w / 2, cy = h * 0.45;
    const cam = 3.4;
    final rotX = 0.32 + sin(_t * 0.17) * 0.14;
    final cyA = cos(_rotY), syA = sin(_rotY), cxA = cos(rotX), sxA = sin(rotX);

    for (var i = 0; i < _nodes.length; i++) {
      final n = _nodes[i];
      final wob = sin(_t * n.sp + n.ph) * jitter * n.amp * (n.dust ? 3 : 1);
      final wave = ripple * 0.06 * sin(n.r * 11 - _t * 7);
      final k = scale * (1 + wob + wave);
      final x = n.x * k, y = n.y * k, z = n.z * k;
      final x1 = x * cyA + z * syA;
      final z1 = -x * syA + z * cyA;
      final y1 = y * cxA - z1 * sxA;
      final z2 = y * sxA + z1 * cxA;
      final p = cam / (cam - z2);
      _sx[i] = cx + x1 * p * R;
      _sy[i] = cy + y1 * p * R;
      _sd[i] = ((z2 + 1.4) / 2.6).clamp(0.15, 1.0);
    }

    // halo
    canvas.drawCircle(
        Offset(cx, cy),
        R * 1.6,
        Paint()
          ..blendMode = BlendMode.plus
          ..shader = ui.Gradient.radial(Offset(cx, cy), R * 1.6,
              [Color.fromRGBO(40, 170, 230, 0.12 * glow.clamp(0, 2)), const Color(0x00000000)]));

    // edges, batched into alpha buckets
    const buckets = 8;
    final paths = List.generate(buckets, (_) => Path());
    for (final (i, j, wgt) in _edges) {
      final a = line * wgt * (_sd[i] + _sd[j]) * 0.5;
      final b = min(buckets - 1, (a * buckets).floor());
      if (b < 1) continue;
      paths[b]
        ..moveTo(_sx[i], _sy[i])
        ..lineTo(_sx[j], _sy[j]);
    }
    final edgePaint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 0.9
      ..blendMode = BlendMode.plus;
    for (var b = 1; b < buckets; b++) {
      edgePaint.color = Color.fromRGBO(80, 215, 250, (b / buckets) * 0.8);
      canvas.drawPath(paths[b], edgePaint);
    }

    // nodes
    final dot = Paint()..blendMode = BlendMode.plus;
    for (var i = 0; i < _nodes.length; i++) {
      final n = _nodes[i];
      final d = _sd[i];
      final twinkle = 0.75 + 0.25 * sin(_t * 3 * n.sp + n.ph);
      final s = n.size * (0.6 + d * 0.8) * (n.hot ? 1.5 : 1);
      final a = (d * twinkle * (n.dust ? 0.55 : 0.95) * (0.7 + line * 0.5)).clamp(0.0, 1.0);
      dot.color = n.hot ? Color.fromRGBO(200, 250, 255, a) : Color.fromRGBO(90, 225, 255, a);
      canvas.drawRect(Rect.fromCenter(center: Offset(_sx[i], _sy[i]), width: s, height: s), dot);
    }

    // hot core
    final coreR = R * (0.5 + glow * 0.25);
    canvas.drawCircle(
        Offset(cx, cy),
        coreR,
        Paint()
          ..blendMode = BlendMode.plus
          ..shader = ui.Gradient.radial(Offset(cx, cy), coreR, [
            Color.fromRGBO(255, 255, 255, (0.55 + glow * 0.45).clamp(0, 1)),
            Color.fromRGBO(200, 250, 255, (0.35 * glow + 0.15).clamp(0, 1)),
            Color.fromRGBO(60, 200, 245, (0.24 * glow).clamp(0, 1)),
            const Color(0x00000000),
          ], [0, 0.12, 0.4, 1]));
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
