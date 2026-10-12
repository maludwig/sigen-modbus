-- Every notification raised (sigen.ntfy): sent, whether or not it got through, or suppressed
-- (its kind was still in cooldown, so nothing was sent). Only gains rows; nothing here touches
-- the other tables. The topic isn't stored: on a public ntfy server it's the only secret (it
-- lives in the config's "alerts" section).
CREATE TABLE alert_log (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alerted_at  TIMESTAMPTZ NOT NULL,     -- when it was raised / sent (this computer's clock)
    kind        TEXT,                     -- what raised it: a sigen.battery_watch event kind, or 'test'
    was_suppressed BOOLEAN  NOT NULL DEFAULT false,   -- true: not sent (cooldown); no HTTP fields
    title       TEXT,
    message     TEXT        NOT NULL,
    priority    TEXT        CHECK (priority IN ('min', 'low', 'default', 'high', 'urgent')),
    tags        TEXT[]      NOT NULL DEFAULT '{}',
    click       TEXT,                     -- the URL the notification opens when tapped
    http_code   SMALLINT,                 -- ntfy's HTTP status; NULL if nothing answered
    ntfy_id     TEXT,                     -- ntfy's message id, when it accepted the message
    error       TEXT,                     -- NULL if accepted; else a 4xx/5xx response's body, or what went
                                          -- wrong if nothing answered (timeout, no route, …)
    CHECK (NOT was_suppressed OR (http_code IS NULL AND ntfy_id IS NULL AND error IS NULL))
);

-- The cooldown: the last alert of a kind that was sent.
CREATE INDEX alert_log_kind_alerted_at ON alert_log (kind, alerted_at);
