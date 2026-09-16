import 'dart:async';

import 'package:android_intent_plus/android_intent.dart';
import 'package:battery_plus/battery_plus.dart';
import 'package:flutter_contacts/flutter_contacts.dart' show ContactProperty, FlutterContacts;
import 'package:flutter_volume_controller/flutter_volume_controller.dart';
import 'package:installed_apps/installed_apps.dart';
import 'package:manage_calendar_events/manage_calendar_events.dart';
import 'package:notification_listener_service/notification_event.dart';
import 'package:notification_listener_service/notification_listener_service.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:torch_light/torch_light.dart';

import 'store.dart';

Map<String, dynamic> _tool(String name, String description,
        [Map<String, dynamic> props = const {}, List<String> required = const []]) =>
    {
      'type': 'function',
      'function': {
        'name': name,
        'description': description,
        'parameters': {'type': 'object', 'properties': props, 'required': required},
      },
    };

Map<String, dynamic> _str(String d) => {'type': 'string', 'description': d};
Map<String, dynamic> _int(String d) => {'type': 'integer', 'description': d};

/// Things Karen can do on the phone itself.
class PhoneSkills {
  static final tools = [
    _tool('open_phone_app', 'Open an app installed on this phone (WhatsApp, Instagram, YouTube, camera, Spotify...).',
        {'name': _str('app name')}, ['name']),
    _tool('find_contact', "Look up a person's phone number in the phone's contacts.", {'name': _str('contact name')},
        ['name']),
    _tool('read_notifications', "Read the phone's recent notifications, e.g. new WhatsApp or SMS messages.",
        {'app': _str('only this app, e.g. whatsapp, messages, gmail; empty = all')}),
    _tool('flashlight', 'Turn the phone flashlight on or off.', {
      'on': {'type': 'boolean', 'description': 'true to turn on, false to turn off'}
    }, ['on']),
    _tool('phone_volume', 'Set the phone media volume (0-100) or change it.', {
      'level': _int('0-100'),
      'action': {'type': 'string', 'enum': ['up', 'down', 'mute', 'unmute']},
    }),
    _tool('battery_status', "The phone's battery level and whether it is charging."),
    _tool('open_phone_settings', 'Open a settings page on the phone.', {
      'page': {
        'type': 'string',
        'enum': ['wifi', 'bluetooth', 'mobile_data', 'location', 'display', 'sound', 'battery', 'apps', 'general']
      }
    }, ['page']),
    _tool('calendar_events', "Events in the phone's calendar for the next few days.",
        {'days': _int('how many days ahead, default 1 (today)')}),
    _tool('add_calendar_event', "Add an event to the phone's calendar.", {
      'title': _str('event title'),
      'start': _str('start as ISO date-time in local time, e.g. 2026-09-20T10:00'),
      'duration_minutes': _int('length, default 60'),
      'location': _str('optional place'),
    }, ['title', 'start']),
    _tool('remember', 'Save a fact the user wants you to remember permanently (people, dates, preferences).',
        {'fact': _str('the fact as a full sentence')}, ['fact']),
    _tool('forget', 'Delete a remembered fact.', {'fact': _str('words from the fact')}, ['fact']),
    _tool('daily_briefing', "Morning/daily briefing data: today's calendar, battery, and remembered dates. Use for "
        "'good morning', 'brief me', 'what's my day'; also call get_weather and get_news for the full briefing."),
  ];

  static final names = {for (final t in tools) t['function']['name'] as String};

  static String status(String name, Map args) => switch (name) {
        'open_phone_app' => 'opening ${args['name'] ?? 'app'}',
        'find_contact' => 'looking up ${args['name'] ?? 'contact'}',
        'read_notifications' => 'reading notifications',
        'flashlight' => 'flashlight ${args['on'] == true ? 'on' : 'off'}',
        'phone_volume' => 'setting volume',
        'battery_status' => 'checking battery',
        'open_phone_settings' => 'opening ${args['page'] ?? ''} settings',
        'calendar_events' => 'checking your calendar',
        'add_calendar_event' => 'adding to calendar',
        'remember' => 'remembering that',
        'forget' => 'forgetting that',
        'daily_briefing' => 'getting your briefing',
        _ => name.replaceAll('_', ' '),
      };

  static Future<Map<String, dynamic>> run(String name, Map<String, dynamic> args) async {
    String arg(String k) => '${args[k] ?? ''}'.trim();
    int? number(String k) => args[k] is num ? (args[k] as num).round() : int.tryParse(arg(k));
    return switch (name) {
      'open_phone_app' => openApp(arg('name')),
      'find_contact' => findContact(arg('name')),
      'read_notifications' => readNotifications(arg('app')),
      'flashlight' => flashlight(args['on'] == true || arg('on') == 'true'),
      'phone_volume' => volume(number('level'), arg('action')),
      'battery_status' => battery(),
      'open_phone_settings' => openSettings(arg('page')),
      'calendar_events' => calendarEvents(number('days') ?? 1),
      'add_calendar_event' =>
        addEvent(arg('title'), arg('start'), number('duration_minutes') ?? 60, arg('location')),
      'remember' => Future.value(Store.instance.remember(arg('fact'))),
      'forget' => Future.value(Store.instance.forget(arg('fact'))),
      'daily_briefing' => briefing(),
      _ => Future.value({'error': 'unknown phone tool $name'}),
    };
  }

  // --- apps ----------------------------------------------------------------------------
  static List<dynamic>? _apps;

  static Future<Map<String, dynamic>> openApp(String name) async {
    final want = name.toLowerCase().replaceAll(RegExp(r'\b(app|the)\b'), '').trim();
    if (want.isEmpty) return {'error': 'which app?'};
    _apps ??= await InstalledApps.getInstalledApps(excludeSystemApps: false, excludeNonLaunchableApps: true);
    final apps = _apps!;
    String label(dynamic a) => '${a.name}'.toLowerCase();
    final match = apps.where((a) => label(a) == want).firstOrNull ??
        apps.where((a) => label(a).startsWith(want)).firstOrNull ??
        apps.where((a) => label(a).contains(want) || '${a.packageName}'.toLowerCase().contains(want)).firstOrNull;
    if (match == null) {
      return {'error': "no app called '$name' is installed on this phone", 'hint': 'check the name, or open it on the PC'};
    }
    final ok = await InstalledApps.startApp(match.packageName);
    return ok == true ? {'opened': match.name} : {'error': "couldn't open ${match.name}"};
  }

  // --- contacts --------------------------------------------------------------------------
  static Future<Map<String, dynamic>> findContact(String name) async {
    if (!(await Permission.contacts.request()).isGranted) {
      return {'error': 'contacts permission is off; ask the user to allow it in Karen settings'};
    }
    final want = name.toLowerCase().trim();
    final all = await FlutterContacts.getAll(properties: {ContactProperty.phone});
    final words = want.split(RegExp(r'\s+')).where((w) => w.isNotEmpty);
    final matches = all
        .where((c) => c.phones.isNotEmpty && words.every((w) => (c.displayName ?? '').toLowerCase().contains(w)))
        .take(5)
        .map((c) => {'name': c.displayName, 'numbers': c.phones.map((p) => p.number).toSet().toList()})
        .toList();
    if (matches.isEmpty) return {'error': "no contact named '$name' with a phone number"};
    return {'contacts': matches};
  }

  // --- notifications ----------------------------------------------------------------------
  static final _recent = <ServiceNotificationEvent>[];
  static StreamSubscription? _listening;

  /// Keep the latest notifications while Karen runs (Android shows only the active ones otherwise).
  static Future<void> startNotificationListener() async {
    if (_listening != null || !await NotificationListenerService.isPermissionGranted()) return;
    _listening = NotificationListenerService.notificationsStream.listen((e) {
      if (e.hasRemoved == true || e.content.isEmpty) return;
      _recent.add(e);
      if (_recent.length > 60) _recent.removeAt(0);
    });
  }

  static const _appNames = {
    'com.whatsapp': 'WhatsApp', 'com.whatsapp.w4b': 'WhatsApp Business', 'com.google.android.apps.messaging': 'Messages',
    'com.android.mms': 'Messages', 'com.google.android.gm': 'Gmail', 'com.instagram.android': 'Instagram',
    'org.telegram.messenger': 'Telegram', 'com.snapchat.android': 'Snapchat', 'com.linkedin.android': 'LinkedIn',
    'com.google.android.youtube': 'YouTube', 'com.phonepe.app': 'PhonePe', 'net.one97.paytm': 'Paytm',
    'com.google.android.apps.nbu.paisa.user': 'Google Pay', 'com.microsoft.teams': 'Teams',
  };

  static Future<Map<String, dynamic>> readNotifications(String app) async {
    if (!await NotificationListenerService.isPermissionGranted()) {
      return {'error': "notification access is off. Tell the user to open Karen's settings and allow notification access."};
    }
    await startNotificationListener();
    final active = await NotificationListenerService.getActiveNotifications();
    final seen = <String>{};
    final want = app.toLowerCase();
    final items = <Map<String, dynamic>>[];
    for (final n in [..._recent.reversed, ...active]) {
      final pkg = n.packageName;
      if (pkg == 'com.koushik.anvi' || n.onGoing == true) continue;
      final appName = _appNames[pkg] ?? pkg.split('.').last;
      if (want.isNotEmpty && !appName.toLowerCase().contains(want) && !pkg.contains(want)) continue;
      final key = '$pkg|${n.title}|${n.content}';
      if (n.content.isEmpty || !seen.add(key)) continue;
      items.add({'app': appName, 'from': n.title, 'text': n.content});
      if (items.length >= 15) break;
    }
    return items.isEmpty ? {'notifications': [], 'note': 'no new notifications'} : {'notifications': items};
  }

  // --- flashlight, volume, battery, settings --------------------------------------------------------
  static Future<Map<String, dynamic>> flashlight(bool on) async {
    try {
      if (!await TorchLight.isTorchAvailable()) return {'error': 'this phone has no flashlight'};
      on ? await TorchLight.enableTorch() : await TorchLight.disableTorch();
      return {'flashlight': on ? 'on' : 'off'};
    } catch (e) {
      return {'error': "couldn't switch the flashlight: $e"};
    }
  }

  static Future<Map<String, dynamic>> volume(int? level, String action) async {
    if (level != null) {
      await FlutterVolumeController.setVolume(level.clamp(0, 100) / 100);
    } else if (action == 'up') {
      await FlutterVolumeController.raiseVolume(0.15);
    } else if (action == 'down') {
      await FlutterVolumeController.lowerVolume(0.15);
    } else if (action == 'mute' || action == 'unmute') {
      await FlutterVolumeController.setMute(action == 'mute');
    } else {
      return {'error': 'give a level 0-100 or up/down/mute'};
    }
    final now = await FlutterVolumeController.getVolume();
    return {'volume_percent': ((now ?? 0) * 100).round()};
  }

  static Future<Map<String, dynamic>> battery() async {
    final b = Battery();
    final state = await b.batteryState;
    return {'battery_percent': await b.batteryLevel, 'state': state.name, 'saver_on': await b.isInBatterySaveMode};
  }

  static const _settingsActions = {
    'wifi': 'android.settings.WIFI_SETTINGS',
    'bluetooth': 'android.settings.BLUETOOTH_SETTINGS',
    'mobile_data': 'android.settings.DATA_ROAMING_SETTINGS',
    'location': 'android.settings.LOCATION_SOURCE_SETTINGS',
    'display': 'android.settings.DISPLAY_SETTINGS',
    'sound': 'android.settings.SOUND_SETTINGS',
    'battery': 'android.intent.action.POWER_USAGE_SUMMARY',
    'apps': 'android.settings.APPLICATION_SETTINGS',
    'general': 'android.settings.SETTINGS',
  };

  static Future<Map<String, dynamic>> openSettings(String page) async {
    final action = _settingsActions[page];
    if (action == null) return {'error': 'unknown settings page'};
    await AndroidIntent(action: action, flags: [0x10000000]).launch(); // FLAG_ACTIVITY_NEW_TASK
    return {'opened': '$page settings', 'note': "Android doesn't let apps switch Wi-Fi/Bluetooth directly; the user taps the switch"};
  }

  // --- calendar ----------------------------------------------------------------------------------
  static final _calendar = CalendarPlugin();

  static Future<bool> _calendarAllowed() async => (await Permission.calendarFullAccess.request()).isGranted;

  static Future<Map<String, dynamic>> calendarEvents(int days) async {
    if (!await _calendarAllowed()) return {'error': 'calendar permission is off'};
    final now = DateTime.now();
    final start = DateTime(now.year, now.month, now.day);
    final end = start.add(Duration(days: days.clamp(1, 31)));
    final events = <Map<String, dynamic>>[];
    for (final cal in await _calendar.getCalendars() ?? []) {
      for (final e in await _calendar.getEventsByDateRange(calendarId: cal.id!, startDate: start, endDate: end) ?? []) {
        events.add({
          'title': e.title,
          'start': e.startDate?.toIso8601String().substring(0, 16),
          'end': e.endDate?.toIso8601String().substring(0, 16),
          if ((e.location ?? '').isNotEmpty) 'location': e.location,
        });
      }
    }
    events.sort((a, b) => '${a['start']}'.compareTo('${b['start']}'));
    return {'from': start.toIso8601String().substring(0, 10), 'days': days, 'events': events};
  }

  static Future<Map<String, dynamic>> addEvent(String title, String start, int minutes, String location) async {
    if (!await _calendarAllowed()) return {'error': 'calendar permission is off'};
    final begin = DateTime.tryParse(start);
    if (begin == null) return {'error': 'start must be a date-time like 2026-09-20T10:00'};
    final calendars = (await _calendar.getCalendars() ?? []).where((c) => c.isReadOnly != true).toList();
    if (calendars.isEmpty) return {'error': 'no writable calendar on this phone'};
    final primary = calendars.firstWhere((c) => '${c.accountName}'.contains('@'), orElse: () => calendars.first);
    final id = await _calendar.createEvent(
      calendarId: primary.id!,
      event: CalendarEvent(
        title: title,
        startDate: begin,
        endDate: begin.add(Duration(minutes: minutes <= 0 ? 60 : minutes)),
        location: location.isEmpty ? null : location,
      ),
    );
    return id == null ? {'error': "couldn't add the event"} : {'added': title, 'at': start, 'calendar': primary.name};
  }

  // --- briefing -------------------------------------------------------------------------------------
  static Future<Map<String, dynamic>> briefing() async {
    final out = <String, dynamic>{};
    try {
      out['today'] = (await calendarEvents(1))['events'];
    } catch (_) {}
    try {
      out['battery'] = await battery();
    } catch (_) {}
    out['remembered'] = Store.instance.memories.map((m) => m['fact']).toList().reversed.take(15).toList();
    out['how_to_answer'] = 'A warm spoken briefing under 90 words: greeting, weather, 2-3 headlines, today\'s '
        'events, and any remembered dates coming up soon.';
    return out;
  }
}
