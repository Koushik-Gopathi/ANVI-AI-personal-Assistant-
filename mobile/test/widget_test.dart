import 'package:anvi/assistant.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('wake and sleep phrases', () {
    expect(hasWakeWord('ANVI.'), isTrue);
    expect(hasWakeWord('The Indus Valley civilization'), isFalse);
    expect(hasWakeWord('I envy you so much'), isFalse);
    expect(isSleepCommand('Bye, ANVI.'), isTrue);
    expect(isSleepCommand('ANVI, what is the time?'), isFalse);
    expect(afterWakeWord('ANVI, what is the time?'), 'what is the time?');
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
