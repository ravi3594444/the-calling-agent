# Tableline: hackathon demo

## The problem

During a restaurant's busiest hours, answering a call takes attention away from
guests in the room. Callers need a table, menu advice, or a simple answer about
their visit. A transcript alone does not finish that job.

## The product

Tableline is a restaurant voice concierge. A caller speaks naturally in a browser;
the host uses restaurant tools to check tables and menu details, then returns a
reservation reference after the caller agrees. The UI makes listening, thinking,
speaking, tool activity, and booking outcomes visible.

## A 90-second presentation

1. Explain the problem: the same small team serves the dining room and handles calls.
2. Open the call studio. Show the restaurant context and the two clearly labeled modes.
3. In live mode, ask for a table for four tomorrow at 7 pm. Interrupt to change the time.
4. Give a name and agree to the booking. Point to the receipt from the executed tool.
5. Ask for vegetarian recommendations. Show the menu lookup beside the conversation.
6. End the call and export the transcript. Explain exactly what reply wait measures.

If live credentials or audio equipment are unavailable, use Interactive demo.
Select a booking, change the time to 7:30, confirm, then cancel it. All demo actions,
receipts and visual responses are explicitly simulated. The demo is text-based
and makes no external voice API call.

## What is implemented

- Live browser microphone and bidirectional voice streaming through the existing backend.
- Speaking animation driven by actual playback amplitude in live mode.
- Readiness, mute, audio output controls, interruption, cancellation and session resume.
- Live transcript, action progress, structured reservation receipts and explicit JSON export.
- Local scripted demo, mobile layout, keyboard controls and reduced-motion handling.
- Ordered tool execution without blocking the audio reader; cleanup and duplicate-call protection.
- Atomic capacity checks within the current in-memory process.

## What a submission must not claim

There is no telephone-number integration, persistent multi-instance reservation
database, or measured production latency result in this build. The reply-wait
indicator is an observed client-side interval, with its method explained in the UI.
The scripted demo has no real booking side effects.

## Run and verify

Python 3.11+ is required. Use Node 24.15+ (or the compatible versions listed in
package.json) for frontend tests. Serving the app does not require Node.

```sh
make install
npm ci
make test
make test-ui
make lint
npm run format:check
make dev
```

Open http://localhost:8080. Without a server API key, the page selects the
interactive demo. For a live call, configure ASSEMBLYAI_API_KEY using .env.example
and restart the app. Microphone access needs localhost or HTTPS.

Before presenting live, test the chosen voice, interruptions, a complete booking,
a cancellation, and reconnect on the actual deployment and device. Existing
bookings disappear on restart and are not shared across server instances.
