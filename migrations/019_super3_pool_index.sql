-- Admin "IDs in Super 3" list and the member's status (Vinay, 26 Oct): every journey is joined to its spot on
-- its current board. Without this index that join reads the whole spots table.
CREATE INDEX IF NOT EXISTS super3_nodes_journey_idx ON super3_nodes (journey_id, board);
