import 'package:flutter/material.dart';
import 'package:notification_listener_service/notification_listener_service.dart';
import 'package:permission_handler/permission_handler.dart';

import 'assistant.dart';
import 'background.dart';
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
