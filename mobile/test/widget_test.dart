import 'package:anvi/assistant.dart';
import 'package:anvi/store.dart';
import 'package:anvi/voice.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('wake and sleep phrases', () {
    expect(hasWakeWord('Karen.'), isTrue);
    expect(hasWakeWord('Hey, Karan'), isTrue);
    expect(hasWakeWord('I am caring for my career'), isFalse);
    expect(isSleepCommand('By Karen.'), isTrue);
    expect(isSleepCommand('Karen, run a speed test'), isFalse);
    expect(afterWakeWord('Hey, Karen. What is the time?'), 'What is the time?');
  });

  test('custom wake word from settings', () {
    Store.instance.setWakeWord('Jarvis');
    expect(hasWakeWord('Hey Jarvis, what time is it'), isTrue);
    expect(hasWakeWord('Karen'), isFalse);
    expect(isSleepCommand('bye Jarvis'), isTrue);
    expect(afterWakeWord('Jarvis, open Instagram please'), 'open Instagram please');
    Store.instance.setWakeWord('Karen');
    expect(hasWakeWord('Karan'), isTrue);
  });

  test('Telugu and Hindi replies are detected for the right voice', () {
    expect(Deepgram.indicLanguage('ఈ రోజు వాతావరణం బాగుంది'), 'te-IN');
    expect(Deepgram.indicLanguage('आज मौसम अच्छा है'), 'hi-IN');
    expect(Deepgram.indicLanguage('The weather is nice'), '');
    expect(Deepgram.cleanForSpeech('ఉష్ణోగ్రత 30°C'), contains('డిగ్రీలు'));
  });

  test('code blocks are split from spoken text', () {
    final (spoken, blocks) = Assistant.splitCode('Here you go.\n```python hello.py\nprint(1)\n```');
    expect(spoken, 'Here you go.');
    expect(blocks.single.filename, 'hello.py');
    expect(blocks.single.code, 'print(1)');
  });

  test('visible text hides code while streaming', () {
    expect(Assistant.visibleText('Sure. ```py\nx = 1', isFinal: false), ('Sure. ', true));
    expect(Assistant.visibleText('Hello there.', isFinal: false), ('Hello there.', false));
  });
}
