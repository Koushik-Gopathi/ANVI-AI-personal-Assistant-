import 'package:flutter/services.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';

/// Keeps Karen's microphone alive while the app is in Recents or the screen is off,
/// using an Android foreground service (shows a "Karen is listening" notification).
class BackgroundListening {
  static bool _initialized = false;

  static void _init() {
    if (_initialized) return;
    _initialized = true;
    FlutterForegroundTask.init(
      androidNotificationOptions: AndroidNotificationOptions(
        channelId: 'karen_listening',
        channelName: 'Karen listening',
        channelDescription: 'Shown while Karen listens for her name in the background',
      ),
      iosNotificationOptions: const IOSNotificationOptions(showNotification: false),
      foregroundTaskOptions: ForegroundTaskOptions(
        eventAction: ForegroundTaskEventAction.nothing(),
        allowWakeLock: true,
        allowWifiLock: true,
      ),
    );
  }

  /// Must be called while the app is on screen (Android only allows starting a
  /// microphone service from the foreground).
  static Future<bool> start(String name) async {
    _init();
    if (await FlutterForegroundTask.checkNotificationPermission() != NotificationPermission.granted) {
      await FlutterForegroundTask.requestNotificationPermission();
    }
    if (await FlutterForegroundTask.isRunningService) return _keepAlive(true).then((_) => true);
    final result = await FlutterForegroundTask.startService(
      serviceId: 7,
      serviceTypes: [ForegroundServiceTypes.microphone],
      notificationTitle: '$name is listening',
      notificationText: 'Say "$name" any time. Turn this off in settings.',
      notificationInitialRoute: '/',
    );
    final ok = result is ServiceRequestSuccess;
    await _keepAlive(ok);
    return ok;
  }

  static Future<void> stop() async {
    _init();
    await _keepAlive(false);
    if (await FlutterForegroundTask.isRunningService) await FlutterForegroundTask.stopService();
  }

  /// Keep Karen's engine running when the app is swiped away (see MainActivity).
  static Future<void> _keepAlive(bool on) async {
    try {
      await const MethodChannel('karen/share').invokeMethod('keepAlive', on);
    } catch (_) {}
  }

  static Future<bool> get batteryUnrestricted => FlutterForegroundTask.isIgnoringBatteryOptimizations;
  static Future<bool> askBatteryUnrestricted() => FlutterForegroundTask.requestIgnoreBatteryOptimization();
}
