-- Who a text came from. Every message so far went OUT, so `to_address` was
-- the whole story; a guest replying "C" is the first message that arrives,
-- and the number it arrived from is the one fact that says whose booking it
-- is about.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS from_address text NOT NULL DEFAULT '';
