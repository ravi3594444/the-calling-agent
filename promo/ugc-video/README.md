# Tableline UGC film

A 34-second vertical (1080×1920, 30 fps) UGC-style ad for Tableline: a busy Saturday night,
missed calls, then the AI host taking a real booking in the call studio.

- `tableline-ugc.mp4`: the render, H.264 with an AAC soundtrack.
- `index.html`: the film itself. Every frame is drawn on a canvas by `draw(frame)`, and the
  music and sound effects are synthesized with Web Audio in the same file. Open it in a browser
  to watch it live, and click to restart it with sound.
- `render.mjs`: seeks the page frame by frame into the MP4 and runs the self-checks.

The method is the one in iArt's
[“every frame drawn in JavaScript”](https://www.iart.ai/blog/ai-javascript-animation) guide and
its open-source [javascript-animation skills](https://github.com/iart-ai/javascript-animation-skills):
the storyboard is data, one timeline drives both picture and sound, and the result was checked
against contact sheets before delivery. The page loads no images, video or audio. The only
files it loads are the app's own Manrope fonts from `static/fonts`.

## Render it

Needs Node 18+, Playwright with Chromium, and ffmpeg with libx264.

```sh
node promo/ugc-video/render.mjs promo/ugc-video/tableline-ugc.mp4   # full render with sound
node promo/ugc-video/render.mjs --sheet 0.5                         # contact sheet -> out/sheet.jpg
node promo/ugc-video/render.mjs --stills 7.2,21.4                   # stills at those seconds -> out/
node promo/ugc-video/render.mjs --check                             # layout, assets, loudness
```

`--check` reports text colliding with text or with key UI, anything loaded besides the fonts,
and any cut that jumps more than 6 dB louder than the music before it (the same rule as the
skill's sync check). The current render passes all three.

## Storyboard

120 bpm. A bar is two seconds, and every cut lands on a downbeat.

| # | Time | What happens | Caption | Sound |
|---|---|---|---|---|
| 1 | 0–2 s | Close-up of the phone on the host stand, ringing. Nobody picks up. | POV: it’s Saturday, 8 PM / and we’re **slammed.** | two-tone ring ×2, a light beat |
| 2 | 2–4 s | Missed calls stack up on the lock screen, one per beat. | the phone / won’t / **stop.** | a falling blip per missed call |
| 3 | 4–6 s | The camera pulls out of the phone into a packed room. The host is seating a couple, servers are running plates, and the phone rings again. | my team’s hands / are **full.** | whoosh, a softer ring |
| 4 | 6–8 s | The camera pushes in: the call was a booking (“Table for 4 · tomorrow · 7:30 PM”), and it gets struck out. | every missed call / = a **lost booking.** | the music drops out, a clap roll and riser build |
| 5 | 8–10 s | The drop: the tableline. logo builds over the voice orb, then flies into a phone’s header. | so we got / an **AI host.** | the full groove comes in, pops on the logo bars |
| 6 | 10–12 s | The call studio, redrawn from the app. A guest taps Start conversation, and the host greets them. | guests tap **start** / on our site… / …and just **talk.** | tap, connected chime |
| 7 | 12–14 s | The orb listens, then the guest’s words transcribe live. | it **listens**… | whoosh, blip |
| 8 | 14–16 s | `Checking table availability` runs and returns the restaurant tool’s answer. | checks our tables / in **real time** | tool click, ding |
| 9 | 16–18 s | The host answers. | answers like / our **best host.** | blip |
| 10 | 18–20 s | The guest cuts in mid-sentence to change the time. | change of plan? / just **cut in.** | clack on the interruption |
| 11 | 20–22 s | The receipt: Alex, 4 guests, 2026-09-27 · 19:30, reference K7M4Q. | **booked.** / reference code and all. | confirmation chime |
| 12 | 22–24 s | “What’s good for vegetarians?” The host names two dishes and flags that both contain dairy. | knows the menu. / even **allergens.** | tool click, ding |
| 13 | 24–26 s | The Arjun · Hindi / English voice, then a Hinglish question and answer about spice. | it even speaks / **Hinglish.** | tool click, ding |
| 14 | 26–28 s | The camera pulls out, and the three feature cards from the app’s “The experience” strip slide in. | one call. / **three things** off our plate. | three pops |
| 15 | 28–30 s | Back in the room: the host walks a party of four to their table, a server greets them, and the phone stays quiet. | so my team stays / with the **guests.** | a pop as their candle lights |
| 16 | 30–34 s | End card: logo, “Busy tables. Never a missed welcome.”, and a Try the interactive demo button. | (end card) | the groove resolves and fades |

## What is real, and what is not claimed

The screens use the app’s own copy and data. That includes the default greeting (“Thanks for
calling The Copper Kettle, this is Meera…”), the phase titles, the tool names from `app.js`,
the tool result format from `restaurant.py`, a reference code from its alphabet (no 0, O, 1 or
I), and the menu facts in `menu.py` (Palak Paneer is spice 0 and contains dairy).

The film also stays inside the limits `docs/HACKATHON.md` lists:

- **No phone-number integration is claimed.** The problem shots show the restaurant’s phone,
  but the solution is a guest starting a conversation “on our site”, which is what this build
  does.
- **No latency figure is shown.** Reply wait reads “—”.
- The lost booking and the party arriving are illustrations, not features. The app doesn’t push
  bookings to the restaurant’s phone, and the film doesn’t show it doing so.

The conversation is compressed to fit the edit, so the call timestamps run fast.

## Changing it

Everything is in `index.html`, near the top:

| To change | Edit |
|---|---|
| Caption wording or timing | `CAPTIONS`. Wrap a phrase in `[brackets]` to highlight it. |
| What’s said in the app | `TURNS`, which are typed in word by word, plus `RECEIPT` |
| The restaurant name on screen | the `callPanel()` and `TURNS` strings |
| Shot timing | `SHOTS`, with matching keys in `studioCam()` / `floorCam()` |
| Sound effects | `EVENTS` (seconds, effect name) |
| Music | `LAYERS` per bar, plus the chord tables above them |

Then run `render.mjs --check` and re-render. `draw(frame)` has to stay a pure function of the
frame number: no `Math.random`, `Date`, or animation state.

## Posting notes

- The captions sit in the upper third, and key UI stays above the lower quarter, which clears
  the TikTok, Reels and Shorts interface.
- The soundtrack is music and sound effects only. A creator voiceover, or a real call recorded
  in live mode, can be laid over it. The captions carry the story if it plays muted.
- Credits: the method and the adapted groove template come from
  [iart-ai/javascript-animation-skills](https://github.com/iart-ai/javascript-animation-skills)
  (MIT License, Copyright (c) 2026 iart.ai). Manrope is under the SIL Open Font License
  (`static/fonts/OFL.txt`).
