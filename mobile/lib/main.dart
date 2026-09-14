import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import 'assistant.dart';
import 'config.dart';
import 'orb.dart';

const bg = Color(0xFF080B14);
const cyan = Color(0xFF5FE3FF);
const textColor = Color(0xFFD6E4EE);
const muted = Color(0xFF6C7A88);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    systemNavigationBarColor: bg,
    statusBarIconBrightness: Brightness.light,
  ));
  final cfg = await AnviConfig.load();
  runApp(AnviApp(cfg: cfg));
}

class AnviApp extends StatefulWidget {
  final AnviConfig cfg;
  const AnviApp({super.key, required this.cfg});

  @override
  State<AnviApp> createState() => _AnviAppState();
}

class _AnviAppState extends State<AnviApp> {
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ANVI',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: bg,
        colorScheme: const ColorScheme.dark(primary: cyan, surface: Color(0xFF0B111D)),
        useMaterial3: true,
      ),
      home: widget.cfg.ready
          ? HomeScreen(cfg: widget.cfg)
          : SetupScreen(cfg: widget.cfg, onDone: () => setState(() {})),
    );
  }
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
class SetupScreen extends StatefulWidget {
  final AnviConfig cfg;
  final VoidCallback onDone;
  const SetupScreen({super.key, required this.cfg, required this.onDone});

  @override
  State<SetupScreen> createState() => _SetupScreenState();
}

class _SetupScreenState extends State<SetupScreen> {
  late final _groq = TextEditingController(text: widget.cfg.groqKey);
  late final _deepgram = TextEditingController(text: widget.cfg.deepgramKey);
  bool _manual = false;

  Future<void> _scan() async {
    final raw = await Navigator.of(context).push<String>(MaterialPageRoute(builder: (_) => const ScanScreen()));
    if (raw == null || !mounted) return;
    if (widget.cfg.applySetupCode(raw)) {
      await widget.cfg.save();
      if (!mounted) return;
      Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => HomeScreen(cfg: widget.cfg)));
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("That isn't an ANVI setup code. On the PC open 📱 → Android app.")));
    }
  }

  Future<void> _saveManual() async {
    widget.cfg
      ..groqKey = _groq.text.trim()
      ..deepgramKey = _deepgram.text.trim();
    if (!widget.cfg.ready) return;
    await widget.cfg.save();
    if (!mounted) return;
    Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => HomeScreen(cfg: widget.cfg)));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(28, 60, 28, 28),
          children: [
            const Text('ANVI', style: TextStyle(color: cyan, letterSpacing: 8, fontSize: 14)),
            const SizedBox(height: 18),
            const Text('Set up your assistant', style: TextStyle(fontSize: 26, fontWeight: FontWeight.w300)),
            const SizedBox(height: 14),
            const Text(
              'On your laptop, open ANVI and tap the 📱 button, choose the "Android app" tab, then scan the code. '
              'That brings over your API keys and lets this phone control your PC.',
              style: TextStyle(color: Color(0xFFB8C7D3), height: 1.5),
            ),
            const SizedBox(height: 28),
            FilledButton.icon(
              onPressed: _scan,
              icon: const Icon(Icons.qr_code_scanner),
              label: const Padding(padding: EdgeInsets.all(14), child: Text('Scan setup code from PC')),
            ),
            const SizedBox(height: 18),
            TextButton(
              onPressed: () => setState(() => _manual = !_manual),
              child: Text(_manual ? 'Hide manual setup' : 'Enter API keys by hand instead'),
            ),
            if (_manual) ...[
              TextField(controller: _groq, decoration: const InputDecoration(labelText: 'Groq API key')),
              const SizedBox(height: 12),
              TextField(controller: _deepgram, decoration: const InputDecoration(labelText: 'Deepgram API key')),
              const SizedBox(height: 18),
              OutlinedButton(onPressed: _saveManual, child: const Text('Save')),
              const SizedBox(height: 8),
              const Text('Without the setup code ANVI works, but cannot control your PC.',
                  style: TextStyle(color: muted, fontSize: 12)),
            ],
          ],
        ),
      ),
    );
  }
}

class ScanScreen extends StatefulWidget {
  const ScanScreen({super.key});

  @override
  State<ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends State<ScanScreen> {
  bool _done = false;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Scan the code on your PC'), backgroundColor: bg),
      body: MobileScanner(
        onDetect: (capture) {
          final value = capture.barcodes.map((b) => b.rawValue).whereType<String>().firstOrNull;
          if (value == null || _done) return;
          _done = true;
          Navigator.of(context).pop(value);
        },
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Home
// ---------------------------------------------------------------------------
class HomeScreen extends StatefulWidget {
  final AnviConfig cfg;
  const HomeScreen({super.key, required this.cfg});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with WidgetsBindingObserver {
  late final Assistant anvi = Assistant(widget.cfg);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    anvi.addListener(() => setState(() {}));
    anvi.onCode.listen((_) => _showCode());
    anvi.startPassive();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Android doesn't allow the mic in the background, so listen only while ANVI is on screen
    if (state == AppLifecycleState.paused) anvi.pause();
    if (state == AppLifecycleState.resumed && anvi.paused) anvi.resume();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    anvi.dispose();
    super.dispose();
  }

  Future<void> _type() async {
    final controller = TextEditingController();
    final text = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFF0B111D),
      builder: (ctx) => Padding(
        padding: EdgeInsets.fromLTRB(16, 16, 16, MediaQuery.of(ctx).viewInsets.bottom + 16),
        child: TextField(
          controller: controller,
          autofocus: true,
          textInputAction: TextInputAction.send,
          onSubmitted: (v) => Navigator.of(ctx).pop(v),
          decoration: InputDecoration(
            hintText: 'Type to ANVI…',
            border: OutlineInputBorder(borderRadius: BorderRadius.circular(30)),
            suffixIcon: IconButton(icon: const Icon(Icons.send), onPressed: () => Navigator.of(ctx).pop(controller.text)),
          ),
        ),
      ),
    );
    if (text != null && text.trim().isNotEmpty) {
      await anvi.ask(text.trim());
    }
  }

  void _showCode() {
    if (anvi.code.isEmpty) return;
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFF0B111D),
      builder: (_) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.7,
        maxChildSize: 0.95,
        builder: (_, scroll) => ListView(
          controller: scroll,
          padding: const EdgeInsets.all(16),
          children: [
            for (final block in anvi.code) ...[
              Row(children: [
                Expanded(
                  child: Text(block.filename ?? (block.lang.isEmpty ? 'code' : block.lang),
                      style: const TextStyle(color: cyan, fontFamily: 'monospace')),
                ),
                TextButton.icon(
                  icon: const Icon(Icons.copy, size: 16),
                  label: const Text('copy'),
                  onPressed: () {
                    Clipboard.setData(ClipboardData(text: block.code));
                    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Copied')));
                  },
                ),
              ]),
              SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: SelectableText(block.code,
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12.5, height: 1.5, color: Color(0xFFCFE3EE))),
              ),
              const SizedBox(height: 24),
            ],
          ],
        ),
      ),
    );
  }

  void _settings() {
    final cfg = widget.cfg;
    showModalBottomSheet(
      context: context,
      backgroundColor: const Color(0xFF0B111D),
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(
            leading: Icon(cfg.hasPc ? Icons.computer : Icons.desktop_access_disabled, color: cyan),
            title: Text(cfg.hasPc ? 'PC control is set up' : 'PC control not set up'),
            subtitle: Text(cfg.hasPc ? [cfg.publicUrl, cfg.lanUrl].where((u) => u.isNotEmpty).join('\n') : 'Scan the setup code from ANVI on your PC'),
          ),
          ListTile(
            leading: const Icon(Icons.qr_code_scanner),
            title: const Text('Scan setup code again'),
            onTap: () {
              Navigator.of(ctx).pop();
              Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => SetupScreen(cfg: cfg, onDone: () {})));
            },
          ),
          ListTile(
            leading: const Icon(Icons.refresh),
            title: const Text('Start a new conversation'),
            onTap: () {
              anvi.forgetConversation();
              Navigator.of(ctx).pop();
            },
          ),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final height = MediaQuery.of(context).size.height;
    return Scaffold(
      body: Stack(children: [
        Positioned.fill(
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: anvi.tap,
            child: Orb(mode: () => anvi.mode, level: () => anvi.level),
          ),
        ),
        SafeArea(
          child: Column(children: [
            const SizedBox(height: 16),
            AnimatedOpacity(
              opacity: anvi.you.isEmpty ? 0 : 1,
              duration: const Duration(milliseconds: 400),
              child: Column(children: [
                const Text('YOU', style: TextStyle(color: muted, fontSize: 10, letterSpacing: 4)),
                const SizedBox(height: 8),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 28),
                  child: Text(anvi.you,
                      textAlign: TextAlign.center,
                      maxLines: 3,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 17, fontStyle: FontStyle.italic, fontWeight: FontWeight.w300)),
                ),
              ]),
            ),
            const Spacer(),
            if (anvi.reply.isNotEmpty)
              ConstrainedBox(
                constraints: BoxConstraints(maxHeight: height * 0.26),
                child: SingleChildScrollView(
                  padding: const EdgeInsets.symmetric(horizontal: 26),
                  child: Text(anvi.reply,
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: Color(0xCCD6E4EE), fontSize: 15, height: 1.55, fontWeight: FontWeight.w300)),
                ),
              ),
            const SizedBox(height: 18),
            Text(anvi.status,
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 14,
                  letterSpacing: 1.5,
                  color: anvi.error != null ? const Color(0xFFFF7A8A) : (anvi.mode == OrbMode.sleep ? muted : cyan),
                )),
            const SizedBox(height: 6),
            Text(anvi.hint.toUpperCase(), style: const TextStyle(fontSize: 9, letterSpacing: 2.5, color: Color(0xFF4A5663))),
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
              child: Row(children: [
                _CornerButton(icon: Icons.tune, onTap: _settings),
                const Spacer(),
                if (anvi.code.isNotEmpty) _CornerButton(icon: Icons.code, onTap: _showCode),
                const SizedBox(width: 8),
                _CornerButton(icon: Icons.keyboard_alt_outlined, onTap: _type),
              ]),
            ),
          ]),
        ),
      ]),
    );
  }
}

class _CornerButton extends StatelessWidget {
  final IconData icon;
  final VoidCallback onTap;
  const _CornerButton({required this.icon, required this.onTap});

  @override
  Widget build(BuildContext context) => IconButton(
        onPressed: onTap,
        icon: Icon(icon, color: const Color(0x995FE3FF), size: 22),
        style: IconButton.styleFrom(
          side: const BorderSide(color: Color(0x265FE3FF)),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      );
}
