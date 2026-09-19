import 'dart:async';
import 'dart:convert';
import 'dart:io' show SocketException;
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';
import 'package:permission_handler/permission_handler.dart';

import 'config.dart';

/// Karen's eyes on the phone: one photo (camera or gallery), one question, one answer.
/// The photo is sent to the vision model for this question and not kept anywhere.
class PhoneVision {
  static const model = 'qwen/qwen3.8-27b';
  static const _prompt = 'You are the eyes of a voice assistant. Answer the question about the image in a few short, '
      'plain sentences that can be read aloud. Copy important text, numbers, amounts, dates and names exactly. If '
      "you can't see or read something clearly, say so instead of guessing.";

  /// True while the camera or gallery is on screen: Karen's app going to the background then is expected.
  static bool busy = false;

  static Future<Map<String, dynamic>> look(AnviConfig cfg, http.Client client, String question, String source) async {
    final fromGallery = source == 'gallery';
    if (!fromGallery && !(await Permission.camera.request()).isGranted) {
      return {'error': 'camera permission is not allowed; allow it in the app settings'};
    }
    XFile? photo;
    busy = true;
    try {
      photo = await ImagePicker().pickImage(
        source: fromGallery ? ImageSource.gallery : ImageSource.camera,
        maxWidth: 1600,
        maxHeight: 1600,
        imageQuality: 82,
      );
    } finally {
      // the app's "resumed" event can arrive just after the picker returns
      Future.delayed(const Duration(seconds: 2), () => busy = false);
    }
    if (photo == null) return {'cancelled': 'the user closed the camera without choosing a photo'};
    return ask(cfg, client, await photo.readAsBytes(), question);
  }

  static Future<Map<String, dynamic>> ask(AnviConfig cfg, http.Client client, Uint8List image, String question) async {
    final mime = image.length > 4 && image[0] == 0x89 && image[1] == 0x50
        ? 'image/png'
        : image.length > 12 && image[8] == 0x57 && image[9] == 0x45
            ? 'image/webp'
            : 'image/jpeg';
    final body = jsonEncode({
      'model': model,
      'max_tokens': 700,
      'temperature': 0.2,
      'messages': [
        {'role': 'system', 'content': _prompt},
        {
          'role': 'user',
          'content': [
            {'type': 'text', 'text': question.trim().isEmpty ? 'What is in this image? Read out any important text.' : question},
            {
              'type': 'image_url',
              'image_url': {'url': 'data:$mime;base64,${base64Encode(image)}'}
            },
          ],
        },
      ],
    });
    for (var attempt = 1;; attempt++) {
      try {
        final r = await client
            .post(Uri.parse('https://api.groq.com/openai/v1/chat/completions'),
                headers: {'Authorization': 'Bearer ${cfg.groqKey}', 'Content-Type': 'application/json'}, body: body)
            .timeout(const Duration(seconds: 90));
        final data = jsonDecode(utf8.decode(r.bodyBytes));
        if (r.statusCode != 200) {
          return {'error': 'vision model failed (${r.statusCode}): ${data['error']?['message'] ?? ''}'};
        }
        final answer = '${data['choices'][0]['message']['content'] ?? ''}'.trim();
        return answer.isEmpty ? {'error': 'the vision model returned nothing'} : {'answer': answer};
      } on Exception catch (e) {
        final network = e is TimeoutException || e is SocketException || e is http.ClientException;
        if (!network || attempt >= 3) {
          return {'error': network ? 'The internet is slow right now; the photo could not be sent.' : '$e'};
        }
        await Future.delayed(Duration(seconds: attempt));
      }
    }
  }
}
