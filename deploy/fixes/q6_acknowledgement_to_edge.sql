-- Move the Q5/Q6 acknowledgement out of the LLM prompt and onto the edge.
--
-- The prompts said:
--     - साफ़ जवाब मिलने पर → "ओके, अगला सवाल।" कहकर 'q6_answered' कॉल करें।
--     ("on a clear answer → SAY "OK, next question." AND call 'q6_answered'")
--
-- Two independent actions, and the model can do the first without the second.
-- On run 16 it did exactly that: no function call fired between the Q5 and Q6
-- transitions, yet TTS spoke the acknowledgement at 07:43:47 and Q6 was asked
-- again. The caller is told their answer landed when it did not.
--
-- transition_speech is played by PipecatEngine.execute_transition, which only
-- runs when a transition actually executes, so the acknowledgement becomes
-- unspeakable without advancing. It is also the single place both the LLM
-- tool-call path and the deterministic fast path go through, so both behave
-- identically.
--
-- Survey order, answer semantics and LLM fallback are untouched: this moves
-- who says a fixed phrase, not when the survey advances.
--
-- Apply inside a transaction and verify before COMMIT.

BEGIN;

-- 1. Attach the acknowledgement to the two answering edges.
UPDATE workflow_definitions
SET workflow_json = jsonb_set(
      workflow_json::jsonb,
      '{edges}',
      (SELECT jsonb_agg(
                CASE
                  WHEN e->>'id' IN ('6-7-q5_answered', '7-8-q6_answered')
                  THEN jsonb_set(e, '{data,transition_speech}',
                                 to_jsonb('ओके, अगला सवाल।'::text), true)
                  ELSE e
                END)
       FROM jsonb_array_elements(workflow_json::jsonb->'edges') e)
    )::json
WHERE id = 2;

-- 2. Stop the prompts instructing the model to speak it. The function-call
--    instruction stays: only the spoken half moves.
UPDATE workflow_definitions
SET workflow_json = jsonb_set(
      workflow_json::jsonb,
      '{nodes}',
      (SELECT jsonb_agg(
                CASE
                  WHEN n->'data'->>'name' IN ('Q5 Vote 2022', 'Q6 Vote 2024')
                  THEN jsonb_set(n, '{data,prompt}',
                         to_jsonb(replace(
                           n->'data'->>'prompt',
                           '"ओके, अगला सवाल।" कहकर ',
                           '')))
                  ELSE n
                END
                ORDER BY (n->>'id')::int)
       FROM jsonb_array_elements(workflow_json::jsonb->'nodes') n)
    )::json
WHERE id = 2;

-- Verify: two edges carry the phrase, no prompt still instructs speaking it,
-- and the graph is unchanged at 9 nodes / 15 edges.
SELECT e->>'id' AS edge, e->'data'->>'transition_speech' AS speech
FROM workflow_definitions d, LATERAL jsonb_array_elements(d.workflow_json::jsonb->'edges') e
WHERE d.id = 2 AND (e->'data'->>'transition_speech') IS NOT NULL;

SELECT n->'data'->>'name' AS node_still_instructing_the_phrase
FROM workflow_definitions d, LATERAL jsonb_array_elements(d.workflow_json::jsonb->'nodes') n
WHERE d.id = 2 AND (n->'data'->>'prompt') LIKE '%"ओके, अगला सवाल।" कहकर%';

SELECT jsonb_array_length(workflow_json::jsonb->'nodes') AS nodes,
       jsonb_array_length(workflow_json::jsonb->'edges') AS edges
FROM workflow_definitions WHERE id = 2;

COMMIT;
