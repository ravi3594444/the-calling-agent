// Deliberately local and scripted. Never acquires a microphone, opens a socket,
// invokes real tools, or emits a live latency measurement.
export class DemoSession {
  constructor(emit) {
    this.emit = emit;
    this.active = false;
    this.timers = new Set();
    this.generation = 0;
    this.phase = 'idle';
  }

  state(phase, message = '') {
    this.phase = phase;
    this.emit({ type: 'state', phase, message, active: this.active, demo: true });
  }

  later(fn, ms) {
    const generation = this.generation;
    const timer = setTimeout(() => {
      this.timers.delete(timer);
      if (this.active && generation === this.generation) fn();
    }, ms);
    this.timers.add(timer);
  }

  start(scenario = null) {
    if (this.active) return;
    this.active = true;
    this.actionId = 0;
    this.receipt = null;
    ++this.generation;
    this.emit({ type: 'session.ready', demo: true });
    this.say(
      'Thanks for calling The Copper Kettle. I can help with a table, the menu, or your visit.',
      () => {
        if (scenario) this.choose(scenario);
        else
          this.choices([
            ['booking', 'A table for four tomorrow at 7?'],
            ['menu', 'What’s good for vegetarians?'],
            ['hours', 'What time do you close?'],
          ]);
      },
    );
  }

  say(text, after) {
    this.state('speaking', 'Scripted host reply · read along in the conversation.');
    this.emit({ type: 'transcript.agent', text, demo: true });
    this.later(
      () => {
        this.state('listening', 'Choose a reply in the conversation panel.');
        after?.();
      },
      Math.min(2200, 700 + text.length * 9),
    );
  }

  choices(items) {
    this.allowed = new Set(items.map(([id]) => id));
    this.emit({ type: 'choices', items });
  }

  action(name, result, next, receipt = null) {
    const call_id = 'demo-' + ++this.actionId;
    this.state('working', 'Simulating the restaurant action…');
    this.emit({ type: 'tool.activity', name, call_id, status: 'started', demo: true });
    this.later(() => {
      this.emit({
        type: 'tool.activity',
        name,
        call_id,
        status: 'completed',
        result,
        receipt,
        demo: true,
      });
      next();
    }, 600);
  }

  choose(choice) {
    if (!this.active) return;
    this.allowed = new Set();
    this.emit({ type: 'choices', items: [] });
    this.actionId ||= 0;
    const texts = {
      booking: 'Could I get a table for four tomorrow at 7 pm?',
      confirm: 'Yes, for Alex. Please book it.',
      later: 'Actually, could we make it 7:30?',
      confirm_later: '7:30 works. Please book it for Alex.',
      menu: 'What’s good for vegetarians?',
      hours: 'What time do you close?',
      cancel: 'Please cancel that reservation.',
    };
    if (choice === 'finish') {
      this.stop();
      return;
    }
    if (!texts[choice]) return;
    this.emit({ type: 'transcript.user', text: texts[choice], demo: true });
    this.state('thinking', 'Scripted walkthrough · preparing the next step.');
    this.later(() => {
      if (choice === 'booking' || choice === 'later') {
        const later = choice === 'later';
        this.action(
          'check_availability',
          'Demo availability: a table for four at ' + (later ? '7:30 pm.' : '7 pm.'),
          () => {
            this.say(
              'There’s a table for four tomorrow at ' +
                (later ? '7:30' : '7') +
                '. Shall I reserve it for you?',
              () => {
                this.choices(
                  later
                    ? [['confirm_later', '7:30 works. Book it for Alex.']]
                    : [
                        ['confirm', 'Yes, for Alex. Please book it.'],
                        ['later', 'Actually, could we make it 7:30?'],
                      ],
                );
              },
            );
          },
        );
      } else if (choice.startsWith('confirm')) {
        const day = new Date();
        day.setDate(day.getDate() + 1);
        this.receipt = {
          name: 'Alex',
          reference: 'DEMO-7K2',
          party_size: 4,
          date:
            day.getFullYear() +
            '-' +
            String(day.getMonth() + 1).padStart(2, '0') +
            '-' +
            String(day.getDate()).padStart(2, '0'),
          time: choice === 'confirm_later' ? '19:30' : '19:00',
          status: 'confirmed',
        };
        this.action(
          'book_table',
          'Example reservation created. This is not a real booking.',
          () => {
            this.say(
              'All set, Alex. Your table for four is reserved in this example. Anything else I can help with?',
              () => {
                this.choices([
                  ['menu', 'What’s good for vegetarians?'],
                  ['cancel', 'Please cancel the example booking.'],
                  ['finish', 'Finish the walkthrough'],
                ]);
              },
            );
          },
          this.receipt,
        );
      } else if (choice === 'menu') {
        this.action(
          'find_dishes',
          'Demo menu match: Palak Paneer and Dal Makhani. Both are vegetarian and contain dairy.',
          () => {
            this.say(
              'Palak Paneer and Dal Makhani are lovely vegetarian options. Both contain dairy. Is there anything you avoid?',
              () => {
                this.choices([
                  ['booking', 'Let’s book a table.'],
                  ['hours', 'What time do you close?'],
                  ['finish', 'Finish the walkthrough'],
                ]);
              },
            );
          },
        );
      } else if (choice === 'hours') {
        this.action(
          'restaurant_info',
          'Example hours: Friday and Saturday, open until 11 pm.',
          () => {
            this.say(
              'In this example, we’re open until 11 pm on Fridays and Saturdays. Would you like a table?',
              () => {
                this.choices([
                  ['booking', 'Yes, a table for four tomorrow at 7.'],
                  ['finish', 'Finish the walkthrough'],
                ]);
              },
            );
          },
        );
      } else if (choice === 'cancel' && this.receipt) {
        this.receipt = { ...this.receipt, status: 'cancelled' };
        this.action(
          'cancel_booking',
          'Example reservation cancelled. No real booking was changed.',
          () => {
            this.say(
              'The example reservation is cancelled. We hope to welcome you another time.',
              () => {
                this.choices([
                  ['booking', 'Try a new booking.'],
                  ['finish', 'Finish the walkthrough'],
                ]);
              },
            );
          },
          this.receipt,
        );
      }
    }, 350);
  }

  reply(choice) {
    if (this.allowed?.has(choice)) this.choose(choice);
  }

  level() {
    // Visual illustration only. The app always marks this mode as scripted.
    return this.active && this.phase === 'speaking'
      ? 0.28 + Math.sin(performance.now() / 133) ** 2 * 0.42
      : 0;
  }

  stop() {
    this.active = false;
    ++this.generation;
    this.timers.forEach(clearTimeout);
    this.timers.clear();
    this.allowed = new Set();
    this.emit({ type: 'choices', items: [] });
    this.state('ended', 'Walkthrough complete. No real reservation was created.');
  }
}
