import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:android_intent_plus/android_intent.dart';
import 'package:http/io_client.dart';
import 'package:url_launcher/url_launcher.dart';

import 'config.dart';

const _browserUa =
    'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36';
const _headers = {'User-Agent': _browserUa, 'Accept-Language': 'en-IN,en;q=0.9'};

// ---------------------------------------------------------------------------
// Tool definitions sent to the model
// ---------------------------------------------------------------------------
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

final phoneTools = [
  _tool(
      'web_search',
      'Search the internet and read the top pages. Use for anything current or uncertain: prices/rates, news, '
          "sports, events, releases, people's current roles, how-to questions.",
      {'query': _str("search query; add city/country and 'today' when relevant")},
      ['query']),
  _tool('get_news', 'Latest news headlines, optionally on a topic.', {'topic': _str('topic, or empty for top stories')}),
  _tool('get_weather', 'Real weather and forecast.', {'location': _str("city; empty = user's location")}),
  _tool('speed_test', "Measure this phone's internet download/upload speed and ping (takes ~15 s)."),
  _tool('open_link', 'Open a website, app link or search on the phone.', {'url': _str('URL, or text to search Google')},
      ['url']),
  _tool('call_number', "Open the phone's dialer with a number ready to call.", {'number': _str('phone number')},
      ['number']),
  _tool('send_sms', 'Open an SMS to a number with the message filled in; the user taps send.',
      {'number': _str('phone number'), 'message': _str('text')}, ['number', 'message']),
  _tool('whatsapp_message', 'Open a WhatsApp chat with the message filled in; the user taps send.',
      {'number': _str('number with country code, e.g. 919876543210'), 'message': _str('text')}, ['number', 'message']),
  _tool('set_alarm', 'Set an alarm on the phone.', {
    'hour': {'type': 'integer', 'description': '0-23'},
    'minute': {'type': 'integer', 'description': '0-59'},
    'label': _str('alarm label'),
  }, ['hour', 'minute']),
  _tool('set_timer', 'Start a countdown timer on the phone.',
      {'seconds': {'type': 'integer', 'description': 'length in seconds'}, 'label': _str('timer label')}, ['seconds']),
  _tool('navigate_to', 'Open Google Maps directions to a place.', {'place': _str('destination')}, ['place']),
];

String toolStatus(String name, Map args) => switch (name) {
      'web_search' => 'searching: ${args['query'] ?? ''}',
      'get_news' => 'checking the news',
      'get_weather' => 'checking the weather',
      'speed_test' => 'running a speed test',
      'open_link' => 'opening link',
      'call_number' => 'opening dialer',
      'send_sms' || 'whatsapp_message' => 'preparing message',
      'set_alarm' => 'setting alarm',
      'set_timer' => 'starting timer',
      'navigate_to' => 'opening maps',
      'run_command' => 'running a command on your PC',
      'open_app' => 'opening ${args['name'] ?? 'app'} on your PC',
      _ => '${name.replaceAll('_', ' ')} on your PC',
    };

// ---------------------------------------------------------------------------
// Runs tools: web/news/weather on the phone, PC actions through Karen on the PC
// ---------------------------------------------------------------------------
class Tools {
  final AnviConfig cfg;
  final http.Client client;
  late final PcBridge pc = PcBridge(cfg);
  Tools(this.cfg, this.client);

  Future<Map<String, dynamic>> run(String name, Map<String, dynamic> args, String userText, int turn,
      String lastReply) async {
    args.removeWhere((_, v) => v == null || v == '');
    String arg(String k) => '${args[k] ?? ''}';
    int number(String k) => (args[k] is num) ? (args[k] as num).round() : int.tryParse(arg(k)) ?? 0;
    try {
      switch (name) {
        case 'web_search':
          return await webSearch(arg('query'));
        case 'get_news':
          return await getNews(arg('topic'));
        case 'get_weather':
          return await getWeather(arg('location'));
        case 'speed_test':
          return await speedTest();
        case 'open_link':
          return await PhoneActions.openLink(arg('url'));
        case 'call_number':
          return await PhoneActions.dial(arg('number'));
        case 'send_sms':
          return await PhoneActions.sms(arg('number'), arg('message'));
        case 'whatsapp_message':
          return await PhoneActions.whatsapp(arg('number'), arg('message'));
        case 'set_alarm':
          return await PhoneActions.alarm(number('hour'), number('minute'), arg('label'));
        case 'set_timer':
          return await PhoneActions.timer(number('seconds'), arg('label'));
        case 'navigate_to':
          return await PhoneActions.navigate(arg('place'));
        default:
          return await pc.call(name, args, userText, turn, lastReply);
      }
    } catch (e) {
      return {'error': '$e'};
    }
  }

  // --- speed test (Cloudflare) ----------------------------------------------------
  Future<Map<String, dynamic>> speedTest() async {
    const base = 'https://speed.cloudflare.com';
    const headers = {'Referer': '$base/'};
    final pings = <double>[];
    for (var i = 0; i < 6; i++) {
      final sw = Stopwatch()..start();
      final r = await client.get(Uri.parse('$base/__down?bytes=0'), headers: headers).timeout(const Duration(seconds: 8));
      if (r.statusCode != 200) return {'error': 'speed test server refused (${r.statusCode})'};
      if (i > 0) pings.add(sw.elapsedMicroseconds / 1000);
    }
    pings.sort();

    // Cloudflare refuses single downloads of 100 MB or more: fetch 25 MB pieces for ~8 s
    var received = 0;
    final down = Stopwatch()..start();
    while (down.elapsedMilliseconds < 8000) {
      final req = http.Request('GET', Uri.parse('$base/__down?bytes=25000000'))..headers.addAll(headers);
      final res = await client.send(req).timeout(const Duration(seconds: 15));
      if (res.statusCode != 200) return {'error': 'download test failed (${res.statusCode})'};
      await for (final chunk in res.stream) {
        received += chunk.length;
        if (down.elapsedMilliseconds > 8000) break;
      }
    }
    final downloadMbps = received * 8 / (down.elapsedMicroseconds / 1e6) / 1e6;

    final blob = List<int>.generate(500000, (i) => (i * 131) & 0xff);
    var sent = 0;
    final up = Stopwatch()..start();
    while (up.elapsedMilliseconds < 6000) {
      final r = await client.post(Uri.parse('$base/__up'), headers: headers, body: blob).timeout(const Duration(seconds: 30));
      if (r.statusCode != 200) break;
      sent += blob.length;
    }
    final uploadMbps = sent * 8 / (up.elapsedMicroseconds / 1e6) / 1e6;
    return {
      'download_mbps': double.parse(downloadMbps.toStringAsFixed(1)),
      'upload_mbps': double.parse(uploadMbps.toStringAsFixed(1)),
      'ping_ms': pings[pings.length ~/ 2].round(),
      'measured_from': 'this phone',
    };
  }

  // --- web search -----------------------------------------------------------
  static const _stop = {
    'the', 'a', 'an', 'of', 'in', 'on', 'for', 'to', 'is', 'are', 'was', 'what', 'who', 'whats', 'how', 'much',
    'many', 'today', 'now', 'current', 'latest', 'me', 'tell', 'and', 'price', 'rate'
  };

  Future<Map<String, dynamic>> webSearch(String query) async {
    List<Map<String, String>> results = [];
    if (cfg.tavilyKey.isNotEmpty) {
      results = await _tavily(query);
    }
    if (results.isEmpty) {
      try {
        final bing = await _bing(query);
        if (_relevance(query, bing) >= 0.6) results = bing;
      } catch (_) {}
    }
    if (results.isEmpty) {
      final news = (await getNews(query))['articles'] as List;
      results = [
        for (final a in news) {'title': '${a['headline']}', 'url': '', 'snippet': '${a['source']}, ${a['published']}'}
      ];
    }
    if (results.isEmpty) return {'query': query, 'error': 'search is unavailable right now'};

    final toRead = results.take(3).where((r) => r['url']!.isNotEmpty && !r.containsKey('page_extract')).toList();
    await Future.wait(toRead.map((r) async {
      final extract = await _pageExtract(r['url']!, query);
      if (extract.isNotEmpty) r['page_extract'] = extract;
    }));
    return {'query': query, 'results': results.take(5).toList()};
  }

  Future<List<Map<String, String>>> _tavily(String query) async {
    try {
      final r = await client
          .post(Uri.parse('https://api.tavily.com/search'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'api_key': cfg.tavilyKey, 'query': query, 'max_results': 5}))
          .timeout(const Duration(seconds: 20));
      if (r.statusCode != 200) return [];
      final data = jsonDecode(utf8.decode(r.bodyBytes));
      return [
        for (final x in data['results'] ?? [])
          {
            'title': '${x['title']}',
            'url': '${x['url']}',
            'snippet': _cut('${x['content']}', 900),
            'page_extract': _cut('${x['content']}', 900),
          }
      ];
    } catch (_) {
      return [];
    }
  }

  Future<List<Map<String, String>>> _bing(String query) async {
    final r = await client
        .get(Uri.https('www.bing.com', '/search', {'q': query, 'cc': 'IN'}), headers: _headers)
        .timeout(const Duration(seconds: 10));
    final page = utf8.decode(r.bodyBytes, allowMalformed: true);
    final results = <Map<String, String>>[];
    for (final block in RegExp(r'<li class="b_algo".*?</li>', dotAll: true).allMatches(page)) {
      final b = block.group(0)!;
      final link = RegExp(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', dotAll: true).firstMatch(b);
      if (link == null) continue;
      final snippet = RegExp(r'<p[^>]*>(.*?)</p>', dotAll: true).firstMatch(b);
      results.add({
        'title': _text(link.group(2)!),
        'url': _unwrapBing(_unescape(link.group(1)!)),
        'snippet': snippet == null ? '' : _cut(_text(snippet.group(1)!), 220),
      });
    }
    return results;
  }

  static String _unwrapBing(String href) {
    if (!href.contains('bing.com/ck/a')) return href;
    var u = Uri.parse(href).queryParameters['u'] ?? '';
    if (!u.startsWith('a1')) return href;
    u = u.substring(2);
    try {
      return utf8.decode(base64Url.decode(base64Url.normalize(u)));
    } catch (_) {
      return href;
    }
  }

  double _relevance(String query, List<Map<String, String>> results) {
    final terms = RegExp(r'[a-z0-9]+')
        .allMatches(query.toLowerCase())
        .map((m) => m.group(0)!)
        .where((w) => w.length > 1 && !_stop.contains(w))
        .toSet();
    if (results.isEmpty) return 0;
    if (terms.isEmpty) return 1;
    var best = 0.0;
    for (final r in results.take(4)) {
      final hay = '${r['title']} ${r['snippet']} ${r['url']}'.toLowerCase();
      final share = terms.where(hay.contains).length / terms.length;
      if (share > best) best = share;
    }
    return best;
  }

  Future<String> _pageExtract(String url, String query) async {
    try {
      final r = await client.get(Uri.parse(url), headers: _headers).timeout(const Duration(seconds: 6));
      if (r.statusCode != 200 || !(r.headers['content-type'] ?? '').contains('html')) return '';
      var raw = utf8.decode(r.bodyBytes, allowMalformed: true);
      if (raw.length > 1500000) raw = raw.substring(0, 1500000);
      raw = raw.replaceAll(
          RegExp(r'<(script|style|noscript|svg|head|nav|footer|form|iframe)[^>]*>.*?</\1>',
              caseSensitive: false, dotAll: true),
          ' ');
      raw = raw.replaceAll(RegExp(r'</t[dh]>', caseSensitive: false), ' | ');
      raw = raw.replaceAll(
          RegExp(r'<br\s*/?>|</(p|div|li|tr|h[1-6]|table|section|article)>', caseSensitive: false), '\n');
      final text = _unescape(raw.replaceAll(RegExp(r'<[^>]+>'), ' '));
      final terms = RegExp(r'[a-z0-9]+').allMatches(query.toLowerCase()).map((m) => m.group(0)!).where((w) => w.length > 2);
      final scored = <(int, int, String)>[];
      final lines = text.split('\n');
      for (var i = 0; i < lines.length; i++) {
        final line = lines[i].replaceAll(RegExp(r'\s+'), ' ').trim();
        if (line.length < 12) continue;
        final low = line.toLowerCase();
        var score = terms.where(low.contains).length * 2 + (RegExp(r'\d').hasMatch(line) ? 2 : 0);
        if (RegExp(r'[₹$€£%]|rs\.?\s*\d').hasMatch(low)) score += 1;
        if (line.length > 400) score -= 2;
        if (score >= 3) scored.add((score, i, _cut(line, 300)));
      }
      scored.sort((a, b) => a.$1 != b.$1 ? b.$1 - a.$1 : a.$2 - b.$2);
      final picked = <(int, String)>[];
      var used = 0;
      for (final s in scored) {
        if (used + s.$3.length > 900) continue;
        picked.add((s.$2, s.$3));
        used += s.$3.length;
      }
      picked.sort((a, b) => a.$1 - b.$1);
      return picked.map((p) => p.$2).join('\n');
    } catch (_) {
      return '';
    }
  }

  // --- news -----------------------------------------------------------------
  Future<Map<String, dynamic>> getNews(String topic) async {
    final uri = topic.isEmpty
        ? Uri.https('news.google.com', '/rss', {'hl': 'en-IN', 'gl': 'IN', 'ceid': 'IN:en'})
        : Uri.https('news.google.com', '/rss/search', {'q': topic, 'hl': 'en-IN', 'gl': 'IN', 'ceid': 'IN:en'});
    final r = await client.get(uri, headers: _headers).timeout(const Duration(seconds: 12));
    final xml = utf8.decode(r.bodyBytes, allowMalformed: true);
    final articles = <Map<String, String>>[];
    for (final m in RegExp(r'<item>(.*?)</item>', dotAll: true).allMatches(xml)) {
      final item = m.group(1)!;
      String tag(String t) =>
          _unescape(RegExp('<$t[^>]*>(.*?)</$t>', dotAll: true).firstMatch(item)?.group(1) ?? '').trim();
      var title = tag('title');
      final source = tag('source');
      if (source.isNotEmpty && title.endsWith(' - $source')) {
        title = title.substring(0, title.length - source.length - 3);
      }
      articles.add({'headline': title, 'source': source, 'published': _cut(tag('pubDate'), 22)});
      if (articles.length >= 8) break;
    }
    return {'topic': topic.isEmpty ? 'top stories India' : topic, 'articles': articles};
  }

  // --- weather --------------------------------------------------------------
  Map<String, dynamic>? _home;

  Future<Map<String, dynamic>?> homeLocation() async {
    if (_home != null) return _home;
    try {
      final r = await client.get(Uri.parse('https://ipwho.is/')).timeout(const Duration(seconds: 8));
      final d = jsonDecode(r.body);
      if (d['success'] == true) {
        _home = {'name': '${d['city']}, ${d['country']}', 'lat': d['latitude'], 'lon': d['longitude']};
      }
    } catch (_) {}
    return _home;
  }

  Future<Map<String, dynamic>?> _geocode(String place) async {
    final r = await client
        .get(Uri.https('geocoding-api.open-meteo.com', '/v1/search', {'name': place, 'count': '1', 'language': 'en'}))
        .timeout(const Duration(seconds: 10));
    final results = jsonDecode(r.body)['results'] as List? ?? [];
    if (results.isEmpty) return null;
    final g = results.first;
    return {
      'name': [g['name'], g['admin1'], g['country']].where((x) => x != null).join(', '),
      'lat': g['latitude'],
      'lon': g['longitude'],
    };
  }

  static const _sky = {
    0: 'clear sky', 1: 'mainly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'fog', 48: 'freezing fog',
    51: 'light drizzle', 53: 'drizzle', 55: 'heavy drizzle', 61: 'light rain', 63: 'rain', 65: 'heavy rain',
    71: 'light snow', 73: 'snow', 75: 'heavy snow', 80: 'light showers', 81: 'showers', 82: 'violent showers',
    95: 'thunderstorm', 96: 'thunderstorm with hail', 99: 'severe thunderstorm with hail',
  };

  Future<Map<String, dynamic>> getWeather(String location) async {
    final loc = location.isEmpty ? await homeLocation() : await _geocode(location);
    if (loc == null) return {'error': location.isEmpty ? 'location unknown; ask for a city' : "can't find $location"};
    final r = await client.get(Uri.https('api.open-meteo.com', '/v1/forecast', {
      'latitude': '${loc['lat']}',
      'longitude': '${loc['lon']}',
      'current': 'temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m',
      'hourly': 'temperature_2m,precipitation_probability,weather_code',
      'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunset',
      'timezone': 'auto',
      'forecast_days': '3',
    })).timeout(const Duration(seconds: 10));
    final d = jsonDecode(r.body);
    final cur = d['current'];
    final hourly = d['hourly'];
    final times = (hourly['time'] as List).cast<String>();
    final nowHour = (cur['time'] as String).substring(0, 13);
    final start = times.indexWhere((t) => t.compareTo(nowHour) >= 0).clamp(0, times.length);
    final daily = d['daily'];
    return {
      'location': loc['name'],
      'local_time': cur['time'],
      'current': {
        'temp_c': cur['temperature_2m'],
        'feels_like_c': cur['apparent_temperature'],
        'humidity': cur['relative_humidity_2m'],
        'wind_kmh': cur['wind_speed_10m'],
        'sky': _sky[cur['weather_code']] ?? 'unknown',
      },
      'next_24h_every_3h': [
        for (var i = start; i < times.length && i < start + 24; i += 3)
          {
            'time': times[i].substring(11, 16),
            'temp_c': hourly['temperature_2m'][i],
            'rain_chance': hourly['precipitation_probability'][i],
            'sky': _sky[hourly['weather_code'][i]] ?? 'unknown',
          }
      ],
      'daily': [
        for (var i = 0; i < (daily['time'] as List).length; i++)
          {
            'date': daily['time'][i],
            'sky': _sky[daily['weather_code'][i]] ?? 'unknown',
            'high_c': daily['temperature_2m_max'][i],
            'low_c': daily['temperature_2m_min'][i],
            'rain_chance': daily['precipitation_probability_max'][i],
          }
      ],
    };
  }

  static String _cut(String s, int n) => s.length > n ? s.substring(0, n) : s;
  static String _text(String html) => _unescape(html.replaceAll(RegExp(r'<[^>]+>'), ' ')).replaceAll(RegExp(r'\s+'), ' ').trim();
  static String _unescape(String s) => s
      .replaceAll('&amp;', '&')
      .replaceAll('&lt;', '<')
      .replaceAll('&gt;', '>')
      .replaceAll('&quot;', '"')
      .replaceAll('&#39;', "'")
      .replaceAll('&#x27;', "'")
      .replaceAll('&nbsp;', ' ')
      .replaceAllMapped(RegExp(r'&#(\d+);'), (m) => String.fromCharCode(int.parse(m.group(1)!)));
}

// ---------------------------------------------------------------------------
// PC bridge: asks Karen on the PC to perform an action
// ---------------------------------------------------------------------------
class PcBridge {
  final AnviConfig cfg;
  late final http.Client _client;
  String? _working;

  PcBridge(this.cfg) {
    final lanHost = Uri.tryParse(cfg.lanUrl)?.host;
    // the PC's local HTTPS certificate is self-made; trust it only for the paired PC's address
    final io = HttpClient()
      ..connectionTimeout = const Duration(seconds: 4)
      ..badCertificateCallback = (cert, host, port) => lanHost != null && host == lanHost;
    _client = IOClient(io);
  }

  /// PC tools Karen can use right now (empty when the PC can't be reached).
  List<Map<String, dynamic>> toolDefs = [];
  DateTime _fetchedAt = DateTime.fromMillisecondsSinceEpoch(0);

  Iterable<String> get _candidates => [?_working, cfg.publicUrl, cfg.lanUrl].where((u) => u.isNotEmpty).toSet();

  Future<void> refreshToolDefs({bool force = false}) async {
    if (!cfg.hasPc) return;
    final age = DateTime.now().difference(_fetchedAt);
    if (!force && age < (toolDefs.isEmpty ? const Duration(seconds: 20) : const Duration(minutes: 3))) return;
    _fetchedAt = DateTime.now();
    for (final base in _candidates) {
      try {
        final r = await _client
            .get(Uri.parse('$base/api/tool-defs'), headers: {'Cookie': 'anvi_pair=${cfg.pairToken}'})
            .timeout(const Duration(seconds: 3));
        if (r.statusCode != 200) continue;
        toolDefs = List<Map<String, dynamic>>.from(jsonDecode(utf8.decode(r.bodyBytes))['tools']);
        _working = base;
        return;
      } catch (_) {
        continue;
      }
    }
    toolDefs = [];
  }

  Future<Map<String, dynamic>> call(String name, Map<String, dynamic> args, String userText, int turn,
      String lastReply) async {
    if (!cfg.hasPc) return {'error': "PC control isn't set up. Scan the setup code from Karen on the PC."};
    for (final base in _candidates) {
      try {
        final r = await _client
            .post(Uri.parse('$base/api/tool'),
                headers: {'Content-Type': 'application/json', 'Cookie': 'anvi_pair=${cfg.pairToken}'},
                body: jsonEncode({
                  'name': name,
                  'args': args,
                  'user_text': userText,
                  'turn': turn,
                  'last_reply': lastReply,
                }))
            .timeout(const Duration(seconds: 120)); // commands and file work can take a while
        if (r.statusCode == 401) return {'error': 'the PC no longer recognises this phone; scan the setup code again'};
        final data = jsonDecode(utf8.decode(r.bodyBytes));
        _working = base;
        return Map<String, dynamic>.from(data['result'] ?? data);
      } catch (_) {
        continue;
      }
    }
    return {
      'error': "Can't reach the PC. Make sure Karen is running on it and the phone is on the same network "
          '(or Tailscale).'
    };
  }
}

// ---------------------------------------------------------------------------
// Phone actions: dialer, SMS, WhatsApp, alarms, timers, maps, links
// ---------------------------------------------------------------------------
class PhoneActions {
  static String _digits(String n) => n.replaceAll(RegExp(r'[^\d+]'), '');

  static Future<Map<String, dynamic>> _open(Uri uri, String what) async {
    final ok = await launchUrl(uri, mode: LaunchMode.externalApplication);
    return ok ? {'opened': what} : {'error': 'no app on this phone can open $what'};
  }

  static Future<Map<String, dynamic>> openLink(String url) {
    final looksLikeUrl = RegExp(r'^[\w+.-]+://|^[\w-]+(\.[\w-]+)+(/\S*)?$').hasMatch(url.trim());
    final uri = looksLikeUrl
        ? Uri.parse(url.contains('://') ? url.trim() : 'https://${url.trim()}')
        : Uri.https('www.google.com', '/search', {'q': url});
    return _open(uri, uri.toString());
  }

  static Future<Map<String, dynamic>> dial(String number) async {
    final r = await _open(Uri(scheme: 'tel', path: _digits(number)), 'the dialer');
    return r.containsKey('error') ? r : {...r, 'note': 'the number is ready; the user taps call'};
  }

  static Future<Map<String, dynamic>> sms(String number, String message) async {
    final r = await _open(Uri(scheme: 'sms', path: _digits(number), queryParameters: {'body': message}), 'messages');
    return r.containsKey('error') ? r : {...r, 'note': 'message is typed in; the user taps send'};
  }

  static Future<Map<String, dynamic>> whatsapp(String number, String message) async {
    final digits = _digits(number).replaceAll('+', '');
    final r = await _open(Uri.https('wa.me', '/$digits', {'text': message}), 'WhatsApp');
    return r.containsKey('error') ? r : {...r, 'note': 'message is typed in; the user taps send'};
  }

  static Future<Map<String, dynamic>> alarm(int hour, int minute, String label) async {
    if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return {'error': 'invalid time'};
    await AndroidIntent(action: 'android.intent.action.SET_ALARM', arguments: {
      'android.intent.extra.alarm.HOUR': hour,
      'android.intent.extra.alarm.MINUTES': minute,
      if (label.isNotEmpty) 'android.intent.extra.alarm.MESSAGE': label,
      'android.intent.extra.alarm.SKIP_UI': true,
    }).launch();
    return {'alarm_set': '${hour.toString().padLeft(2, '0')}:${minute.toString().padLeft(2, '0')}', 'label': label};
  }

  static Future<Map<String, dynamic>> timer(int seconds, String label) async {
    if (seconds <= 0) return {'error': 'invalid length'};
    await AndroidIntent(action: 'android.intent.action.SET_TIMER', arguments: {
      'android.intent.extra.alarm.LENGTH': seconds,
      if (label.isNotEmpty) 'android.intent.extra.alarm.MESSAGE': label,
      'android.intent.extra.alarm.SKIP_UI': true,
    }).launch();
    return {'timer_started_seconds': seconds, 'label': label};
  }

  static Future<Map<String, dynamic>> navigate(String place) =>
      _open(Uri.https('www.google.com', '/maps/dir/', {'api': '1', 'destination': place}), 'Google Maps');
}
