import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:notification_listener_service/notification_listener_service.dart';
import 'package:permission_handler/permission_handler.dart';

import 'assistant.dart';
import 'background.dart';
import 'diag.dart';
import 'orb.dart';
import 'main.dart';
import 'phone_skills.dart';
import 'store.dart';

const _card = Color(0xFF0B111D);
const _sub = TextStyle(color: muted, fontSize: 12);

Widget _heading(String text) => Padding(
      padding: const EdgeInsets.fromLTRB(16, 22, 16, 6),
      child: Text(text.toUpperCase(), style: const TextStyle(color: cyan, fontSize: 11, letterSpacing: 3)),
    );

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
class SettingsScreen extends StatefulWidget {
  final Assistant anvi;
  final VoidCallback onRescan;
  const SettingsScreen({super.key, required this.anvi, required this.onRescan});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> with WidgetsBindingObserver {
  final store = Store.instance;
  late final _wake = TextEditingController(text: store.wakeWord);
  final _granted = <String, bool>{};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _checkPermissions();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _checkPermissions(); // back from Android settings
  }

  Future<void> _checkPermissions() async {
    final values = {
      'mic': await Permission.microphone.isGranted,
      'contacts': await Permission.contacts.isGranted,
      'calendar': await Permission.calendarFullAccess.isGranted,
      'notifications': await NotificationListenerService.isPermissionGranted(),
      'battery': await BackgroundListening.batteryUnrestricted,
      'overlay': await Permission.systemAlertWindow.isGranted,
    };
    if (values['notifications']!) PhoneSkills.startNotificationListener();
    if (mounted) setState(() => _granted.addAll(values));
  }

  Future<void> _save() async {
    await store.save();
    if (mounted) setState(() {});
  }

  void _toast(String text) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  Future<void> _setWakeWord() async {
    final error = store.setWakeWord(_wake.text);
    if (error != null) return _toast(error);
    _wake.text = store.wakeWord;
    FocusScope.of(context).unfocus();
    await _save();
    _toast('Say “${store.wakeWord}” to wake me now');
  }

  Future<void> _setBackground(bool on) async {
    if (on) {
      final started = await BackgroundListening.start(store.wakeWord);
      if (!started) return _toast("Android didn't allow background listening");
      if (!await BackgroundListening.batteryUnrestricted) await BackgroundListening.askBatteryUnrestricted();
    } else {
      await BackgroundListening.stop();
    }
    store.backgroundListening = on;
    await _save();
    _checkPermissions();
  }

  Widget _permission(String key, IconData icon, String title, String why, Future<void> Function() ask) {
    final ok = _granted[key];
    return ListTile(
      leading: Icon(icon, color: ok == true ? cyan : muted),
      title: Text(title),
      subtitle: Text(ok == null ? why : (ok ? 'Allowed' : 'Not allowed — tap to allow. $why'), style: _sub),
      trailing: Icon(ok == true ? Icons.check_circle : Icons.chevron_right, color: ok == true ? cyan : muted),
      onTap: () async {
        await ask();
        await _checkPermissions();
      },
    );
  }

  Future<void> _request(Permission p, String why) async {
    await ensurePermission(context, p, why);
  }

  @override
  Widget build(BuildContext context) {
    final cfg = widget.anvi.cfg;
    final pcOk = widget.anvi.tools.pc.toolDefs.isNotEmpty;
    return Scaffold(
      appBar: AppBar(title: const Text('Settings'), backgroundColor: bg),
      body: ListView(padding: const EdgeInsets.only(bottom: 40), children: [
        _heading('Voice'),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: TextField(
            controller: _wake,
            textCapitalization: TextCapitalization.words,
            onSubmitted: (_) => _setWakeWord(),
            decoration: InputDecoration(
              labelText: 'Name / wake word',
              helperText: 'Say it to wake me, or with “bye” to put me to sleep',
              suffixIcon: IconButton(icon: const Icon(Icons.check), onPressed: _setWakeWord),
            ),
          ),
        ),
        const SizedBox(height: 8),
        ListTile(
          title: const Text('Voice'),
          subtitle: const Text('English replies (Telugu/Hindi use their own voice)', style: _sub),
          trailing: DropdownButton<String>(
            value: store.voice,
            dropdownColor: _card,
            underline: const SizedBox(),
            items: [for (final e in Store.voices.entries) DropdownMenuItem(value: e.key, child: Text(e.value))],
            onChanged: (v) {
              store.voice = v ?? store.voice;
              _save();
            },
          ),
        ),
        ListTile(
          title: const Text('Language'),
          subtitle: const Text('What I listen for and reply in', style: _sub),
          trailing: DropdownButton<String>(
            value: store.language,
            dropdownColor: _card,
            underline: const SizedBox(),
            items: [for (final e in Store.languages.entries) DropdownMenuItem(value: e.key, child: Text(e.value))],
            onChanged: (v) {
              store.language = v ?? store.language;
              _save();
              if (v != 'english' && cfg.sarvamKey.isEmpty) {
                _toast('Telugu/Hindi replies use the phone voice. Add a Sarvam AI key on the laptop for a better one.');
              }
            },
          ),
        ),
        SwitchListTile(
          title: const Text('Speak replies'),
          subtitle: const Text('Off: replies are shown as text only', style: _sub),
          value: store.speakReplies,
          onChanged: (v) {
            store.speakReplies = v;
            _save();
          },
        ),
        SwitchListTile(
          title: const Text('Stop talking when I start speaking'),
          subtitle: const Text('Talk over me to interrupt, or say “stop”. Works best with earphones.', style: _sub),
          value: store.bargeIn,
          onChanged: (v) {
            store.bargeIn = v;
            _save();
          },
        ),
        SwitchListTile(
          title: const Text('Sounds'),
          subtitle: const Text('Small chimes when I wake, start a task, finish, or hit an error', style: _sub),
          value: store.sounds,
          onChanged: (v) {
            store.sounds = v;
            _save();
          },
        ),
        SwitchListTile(
          title: const Text('Listen in the background'),
          subtitle: const Text(
              'Keeps listening for my name when the app is in Recents or the screen is off. '
              'Shows a notification and uses more battery.',
              style: _sub),
          value: store.backgroundListening,
          onChanged: _setBackground,
        ),
        _heading('Permissions'),
        _permission('mic', Icons.mic_none, 'Microphone', 'To hear you.',
            () => _request(Permission.microphone, micWhy)),
        _permission('contacts', Icons.contacts_outlined, 'Contacts', 'To call and message people by name.',
            () => _request(Permission.contacts, 'Karen needs contacts to call or message people by name.')),
        _permission('calendar', Icons.event_outlined, 'Calendar', 'To read and add events.',
            () => _request(Permission.calendarFullAccess, 'Karen needs the calendar to read and add events.')),
        _permission('notifications', Icons.notifications_none, 'Notification access',
            'To read your notifications aloud (e.g. WhatsApp messages).', () async {
          await NotificationListenerService.requestPermission();
        }),
        _permission('overlay', Icons.open_in_new, 'Open apps from the background',
            "So “open WhatsApp” works while I'm in the background (Android calls it “display over other apps”).",
            () async {
          await Permission.systemAlertWindow.request();
        }),
        _permission('battery', Icons.battery_saver_outlined, 'Unrestricted battery',
            'So Android doesn\'t stop background listening.', () async {
          await BackgroundListening.askBatteryUnrestricted();
        }),
        _heading('Memory'),
        if (store.memories.isEmpty)
          const ListTile(
              title: Text('Nothing remembered yet'),
              subtitle: Text('Say “remember that my bike service is on Friday”', style: _sub)),
        for (final m in store.memories.reversed)
          ListTile(
            dense: true,
            title: Text('${m['fact']}'),
            subtitle: Text('saved ${m['added']}', style: _sub),
            trailing: IconButton(
              icon: const Icon(Icons.close, size: 18, color: muted),
              onPressed: () {
                store.memories.remove(m);
                _save();
              },
            ),
          ),
        _heading('Laptop'),
        ListTile(
          leading: Icon(cfg.hasPc ? Icons.computer : Icons.desktop_access_disabled, color: pcOk ? cyan : muted),
          title: Text(!cfg.hasPc
              ? 'Laptop control not set up'
              : pcOk
                  ? 'Connected to your laptop'
                  : "Laptop set up, but can't reach it right now"),
          subtitle: Text(
              cfg.hasPc
                  ? 'Say “on my laptop …” to use it. Karen must be open on the laptop.'
                  : 'Scan the setup code from Karen on your laptop',
              style: _sub),
        ),
        ListTile(
          leading: const Icon(Icons.qr_code_scanner),
          title: const Text('Scan setup code again'),
          onTap: widget.onRescan,
        ),
        _heading('Help'),
        ListTile(
          leading: const Icon(Icons.monitor_heart_outlined),
          title: const Text('Diagnostics'),
          subtitle: const Text("Check that I can hear you, speak, and reach the internet and your laptop", style: _sub),
          onTap: () => Navigator.of(context)
              .push(MaterialPageRoute(builder: (_) => DiagnosticsScreen(anvi: widget.anvi, granted: _granted))),
        ),
        _heading('Conversation'),
        ListTile(
          leading: const Icon(Icons.history),
          title: const Text('Chat history'),
          onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const HistoryScreen())),
        ),
        ListTile(
          leading: const Icon(Icons.refresh),
          title: const Text('Start a new conversation'),
          onTap: () {
            widget.anvi.forgetConversation();
            _toast('Started a new conversation');
          },
        ),
      ]),
    );
  }
}

// ---------------------------------------------------------------------------
// Chat history
// ---------------------------------------------------------------------------
class HistoryScreen extends StatefulWidget {
  const HistoryScreen({super.key});

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  static String _when(int ms) {
    final t = DateTime.fromMillisecondsSinceEpoch(ms);
    final now = DateTime.now();
    final time = '${t.hour % 12 == 0 ? 12 : t.hour % 12}:${t.minute.toString().padLeft(2, '0')} ${t.hour < 12 ? 'AM' : 'PM'}';
    if (t.year == now.year && t.month == now.month && t.day == now.day) return 'Today $time';
    final yesterday = now.subtract(const Duration(days: 1));
    if (t.year == yesterday.year && t.month == yesterday.month && t.day == yesterday.day) return 'Yesterday $time';
    return '${t.day}/${t.month}/${t.year} $time';
  }

  @override
  Widget build(BuildContext context) {
    final items = Store.instance.history.reversed.toList();
    return Scaffold(
      appBar: AppBar(title: const Text('Chat history'), backgroundColor: bg, actions: [
        if (items.isNotEmpty)
          IconButton(
            icon: const Icon(Icons.delete_outline),
            tooltip: 'Clear history',
            onPressed: () async {
              final ok = await showDialog<bool>(
                context: context,
                builder: (ctx) => AlertDialog(
                  backgroundColor: _card,
                  title: const Text('Clear chat history?'),
                  actions: [
                    TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
                    FilledButton(onPressed: () => Navigator.of(ctx).pop(true), child: const Text('Clear')),
                  ],
                ),
              );
              if (ok == true) setState(Store.instance.clearHistory);
            },
          ),
      ]),
      body: items.isEmpty
          ? const Center(child: Text('No conversations yet', style: TextStyle(color: muted)))
          : ListView.builder(
              padding: const EdgeInsets.all(16),
              itemCount: items.length,
              itemBuilder: (_, i) {
                final h = items[i];
                final steps = (h['steps'] as List? ?? []).cast<String>();
                return Container(
                  margin: const EdgeInsets.only(bottom: 14),
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: _card,
                    borderRadius: BorderRadius.circular(14),
                    border: Border.all(color: const Color(0x225FE3FF)),
                  ),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(_when(h['at'] as int), style: const TextStyle(color: muted, fontSize: 11)),
                    const SizedBox(height: 8),
                    SelectableText('${h['you']}', style: const TextStyle(fontStyle: FontStyle.italic)),
                    for (final s in steps)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Text('✓ $s',
                            style: const TextStyle(fontFamily: 'monospace', fontSize: 11, color: Color(0xFF7F93A3))),
                      ),
                    const SizedBox(height: 8),
                    SelectableText('${h['karen']}', style: const TextStyle(color: Color(0xCCD6E4EE), height: 1.45)),
                  ]),
                );
              },
            ),
    );
  }
}

// ---------------------------------------------------------------------------
// Diagnostics: is she hearing me, is she making sound, can she reach the services?
// ---------------------------------------------------------------------------
class DiagnosticsScreen extends StatefulWidget {
  final Assistant anvi;
  final Map<String, bool> granted;
  const DiagnosticsScreen({super.key, required this.anvi, required this.granted});

  @override
  State<DiagnosticsScreen> createState() => _DiagnosticsScreenState();
}

class _Check {
  final String name;
  final bool ok;
  final String detail;
  _Check(this.name, this.ok, this.detail);
}

class _DiagnosticsScreenState extends State<DiagnosticsScreen> {
  late final Timer _timer;
  List<_Check>? _checks;
  String _voiceNote = '';
  bool _background = false;

  Assistant get anvi => widget.anvi;

  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(const Duration(milliseconds: 150), (_) => setState(() {}));
    _runChecks();
  }

  @override
  void dispose() {
    _timer.cancel();
    super.dispose();
  }

  Future<_Check> _timed(String name, Future<(bool, String)> Function() check) async {
    final watch = Stopwatch()..start();
    try {
      final (ok, detail) = await check().timeout(const Duration(seconds: 20));
      return _Check(name, ok, '$detail (${watch.elapsedMilliseconds} ms)');
    } catch (e) {
      return _Check(name, false, e is TimeoutException ? 'no answer in 20 s — internet slow?' : '$e');
    }
  }

  Future<void> _runChecks() async {
    setState(() => _checks = null);
    final cfg = anvi.cfg;
    final client = anvi.client;
    final results = await Future.wait([
      _timed('Groq (brain + eyes)', () async {
        final r = await client.get(Uri.parse('https://api.groq.com/openai/v1/models'),
            headers: {'Authorization': 'Bearer ${cfg.groqKey}'});
        if (r.statusCode == 401) return (false, 'key rejected — scan the setup code again');
        return (r.statusCode == 200, r.statusCode == 200 ? 'reachable' : 'answered ${r.statusCode}');
      }),
      _timed('Deepgram (hearing + voice)', () async {
        final r = await client.get(Uri.parse('https://api.deepgram.com/v1/projects'),
            headers: {'Authorization': 'Token ${cfg.deepgramKey}'});
        if (r.statusCode == 401 || r.statusCode == 403) return (false, 'key rejected — scan the setup code again');
        return (r.statusCode == 200, r.statusCode == 200 ? 'reachable' : 'answered ${r.statusCode}');
      }),
      _timed('Laptop', () async {
        if (!cfg.hasPc) return (false, 'not set up — scan the setup code from Karen on the laptop');
        await anvi.tools.pc.refreshToolDefs(force: true);
        final n = anvi.tools.pc.toolDefs.length;
        return n > 0 ? (true, 'connected, $n laptop actions') : (false, "can't reach it — Karen open on the laptop, same network?");
      }),
    ]);
    _background = await FlutterForegroundTask.isRunningService;
    if (mounted) setState(() => _checks = results);
  }

  Future<void> _testVoice() async {
    setState(() => _voiceNote = 'getting the voice…');
    try {
      final clip = await anvi.deepgram
          .speak('Hi, this is ${Store.instance.wakeWord}. If you can hear me, my voice is working.');
      anvi.player.add(clip);
      setState(() => _voiceNote = 'playing — did you hear it? If not, turn the media volume up.');
    } catch (e) {
      setState(() => _voiceNote = 'voice failed: $e');
    }
  }

  Widget _row(String name, String value, {bool bad = false}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SizedBox(width: 130, child: Text(name, style: const TextStyle(color: muted, fontSize: 12.5))),
          Expanded(
            child: Text(value,
                style: TextStyle(
                    fontFamily: 'monospace', fontSize: 12, color: bad ? const Color(0xFFFF9AA6) : textColor)),
          ),
        ]),
      );

  Widget _meter(String name, double level) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Row(children: [
          SizedBox(width: 130, child: Text(name, style: const TextStyle(color: Color(0xFFB8C7D3)))),
          Expanded(
            child: ClipRRect(
              borderRadius: BorderRadius.circular(6),
              child: LinearProgressIndicator(
                value: level.clamp(0.0, 1.0),
                minHeight: 8,
                backgroundColor: const Color(0x155FE3FF),
                color: cyan,
              ),
            ),
          ),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    final store = Store.instance;
    final mic = anvi.mic;
    String yesNo(bool? v) => v == null ? '…' : (v ? 'allowed' : 'NOT allowed');
    return Scaffold(
      appBar: AppBar(title: const Text('Diagnostics'), backgroundColor: bg),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        const Text("Speak and watch the microphone bar. If it doesn't move, I can't hear you.",
            style: TextStyle(color: muted)),
        const SizedBox(height: 10),
        _meter('Microphone', mic.level * 12),
        _meter('My voice', anvi.player.playing ? 0.6 : 0),
        const SizedBox(height: 10),
        _row('State', '${anvi.mode.name}${anvi.awake ? ' (awake)' : anvi.passive ? ' (asleep, listening for “${store.wakeWord}”)' : ''}'
            '${anvi.paused ? ' · paused (app in background)' : ''}'),
        _row('Microphone', mic.running ? 'on' : 'off', bad: !mic.running && anvi.mode != OrbMode.thinking && anvi.mode != OrbMode.speaking),
        _row('Last heard', Diag.heard.isEmpty ? 'nothing yet' : '“${Diag.heard}” · ${Diag.ago(Diag.heardAt)} · ${Diag.sttMs} ms'),
        _row('Heard while asleep', Diag.wakeHeard.isEmpty ? 'nothing yet' : '“${Diag.wakeHeard}”'),
        _row('Voice clips', '${Diag.clipsPlayed} played${Diag.clipsFailed > 0 ? ', ${Diag.clipsFailed} failed' : ''}',
            bad: Diag.clipsFailed > 0),
        _row('Interrupting', store.bargeIn ? 'on · ${Diag.bargeIns} interruptions, ${Diag.echoesIgnored} echoes ignored' : 'off'),
        _row('Language', '${Store.languages[store.language]}${store.language != 'english' && anvi.cfg.sarvamKey.isEmpty ? ' · phone voice (no Sarvam key)' : ''}'),
        _row('Background listening', store.backgroundListening ? (_background ? 'on, running' : 'on, but NOT running — reopen the app') : 'off',
            bad: store.backgroundListening && !_background),
        _row('Last error', Diag.lastError.isEmpty ? 'none' : '${Diag.lastError} · ${Diag.ago(Diag.lastErrorAt)}',
            bad: Diag.lastError.isNotEmpty),
        const SizedBox(height: 12),
        Wrap(spacing: 10, runSpacing: 8, children: [
          FilledButton.tonal(onPressed: _testVoice, child: const Text('Test voice')),
          FilledButton.tonal(onPressed: _runChecks, child: const Text('Check services again')),
        ]),
        if (_voiceNote.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_voiceNote, style: _sub)),
        _heading('Services'),
        if (_checks == null) const Padding(padding: EdgeInsets.all(8), child: Text('Checking…', style: TextStyle(color: muted))),
        for (final c in _checks ?? <_Check>[])
          ListTile(
            dense: true,
            leading: Icon(c.ok ? Icons.check_circle : Icons.error_outline,
                color: c.ok ? const Color(0xFF4FD19B) : const Color(0xFFFF7A8A)),
            title: Text(c.name),
            subtitle: Text(c.detail, style: _sub),
          ),
        _heading('Permissions'),
        _row('Microphone', yesNo(widget.granted['mic']), bad: widget.granted['mic'] == false),
        _row('Contacts', yesNo(widget.granted['contacts']), bad: widget.granted['contacts'] == false),
        _row('Calendar', yesNo(widget.granted['calendar']), bad: widget.granted['calendar'] == false),
        _row('Notification access', yesNo(widget.granted['notifications']), bad: widget.granted['notifications'] == false),
        _row('Unrestricted battery', yesNo(widget.granted['battery'])),
        _row('Open apps from background', yesNo(widget.granted['overlay'])),
      ]),
    );
  }
}
