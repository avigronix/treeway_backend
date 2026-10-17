-- AdminPins: "Sent" tab and "sent <date>" read the last transfer of each PIN.
CREATE INDEX IF NOT EXISTS pin_transfers_pin_idx ON pin_transfers (pin_id, created_at DESC);
