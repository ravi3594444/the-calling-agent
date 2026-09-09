"""Message type constants for the AssemblyAI Voice Agent WebSocket API.

Kept in one place so a protocol change is a single-file edit.
"""

# ---- Client -> server ----
SESSION_UPDATE = "session.update"      # configuration; must be the first message
SESSION_RESUME = "session.resume"      # reconnect within the 30s window
INPUT_AUDIO = "input.audio"            # base64 audio chunk
TOOL_RESULT = "tool.result"            # answer a tool.call

# ---- Server -> client ----
SESSION_READY = "session.ready"                # carries session_id
SESSION_UPDATED = "session.updated"
INPUT_SPEECH_STARTED = "input.speech.started"  # user began speaking -> barge-in
INPUT_SPEECH_STOPPED = "input.speech.stopped"
TRANSCRIPT_USER_DELTA = "transcript.user.delta"
TRANSCRIPT_USER = "transcript.user"
REPLY_STARTED = "reply.started"
REPLY_AUDIO = "reply.audio"                    # base64 audio chunk
TRANSCRIPT_AGENT = "transcript.agent"
REPLY_DONE = "reply.done"                      # status == "interrupted" on barge-in
TOOL_CALL = "tool.call"
SESSION_ERROR = "session.error"

# ---- Audio encodings ----
# Browser / WebRTC path: 16-bit signed little-endian PCM, 24 kHz, mono.
ENCODING_PCM = "audio/pcm"
PCM_SAMPLE_RATE = 24_000
# Telephony path: G.711 mu-law 8 kHz. Byte-compatible with Telnyx/Twilio
# media streams, so frames forward with zero transcoding.
ENCODING_PCMU = "audio/pcmu"
PCMU_SAMPLE_RATE = 8_000
