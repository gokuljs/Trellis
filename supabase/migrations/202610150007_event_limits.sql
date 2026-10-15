BEGIN;
LOCK TABLE trellis.runs,trellis.run_events IN ACCESS EXCLUSIVE MODE;

ALTER TABLE trellis.runs
    ADD COLUMN event_storage_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN event_storage_bytes bigint NOT NULL DEFAULT 0;
UPDATE trellis.runs r SET event_storage_count=s.event_count,event_storage_bytes=s.data_bytes
FROM (SELECT run_id,count(*) AS event_count,sum(octet_length(data::text)) AS data_bytes
    FROM trellis.run_events GROUP BY run_id) s WHERE r.id=s.run_id;
-- Existing replay is retained. Refuse an incompatible migration rather than
-- deleting history or silently pretending an oversized run fits the new caps.
DO $preflight$
BEGIN
    IF EXISTS(SELECT 1 FROM trellis.runs WHERE event_storage_count>8192 OR event_storage_bytes>16777216
        OR last_event_sequence<>event_storage_count)
       OR EXISTS(SELECT 1 FROM trellis.run_events WHERE octet_length(data::text)>262144) THEN
        RAISE EXCEPTION 'existing run event history exceeds storage limits' USING ERRCODE='22023';
    END IF;
END;
$preflight$;
ALTER TABLE trellis.runs
    ADD CONSTRAINT run_event_count_bounded CHECK(event_storage_count BETWEEN 0 AND 8192),
    ADD CONSTRAINT run_event_sequence_bounded CHECK(last_event_sequence BETWEEN 0 AND 8192),
    ADD CONSTRAINT run_event_bytes_bounded CHECK(event_storage_bytes BETWEEN 0 AND 16777216);
REVOKE SELECT(event_storage_count,event_storage_bytes) ON trellis.runs FROM PUBLIC,anon,authenticated;

CREATE OR REPLACE FUNCTION trellis_private.append_event(
    p_user uuid,p_run_id uuid,p_event_type text,p_data jsonb,p_event_version integer DEFAULT 1
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_run trellis.runs%ROWTYPE;
    v_event trellis.run_events%ROWTYPE;
    v_size bigint:=octet_length(p_data::text);
    v_reserved boolean:=p_event_type IN ('run.cancellation_requested','assistant.completed',
        'run.completed','run.failed','run.cancelled','run.interrupted');
    v_count_limit bigint:=CASE WHEN v_reserved THEN 8192 ELSE 8176 END;
    v_byte_limit bigint:=CASE WHEN v_reserved THEN 16777216 ELSE 16711680 END;
BEGIN
    SELECT * INTO v_run FROM trellis.runs WHERE id=p_run_id AND user_id=p_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'run not found' USING ERRCODE='22023'; END IF;
    IF v_size>262144 OR v_run.event_storage_count+1>v_count_limit
       OR v_run.last_event_sequence+1>v_count_limit
       OR v_run.event_storage_bytes+v_size>v_byte_limit THEN
        RAISE EXCEPTION 'event_storage_limit' USING ERRCODE='22023';
    END IF;
    UPDATE trellis.runs SET last_event_sequence=last_event_sequence+1,
        event_storage_count=event_storage_count+1,event_storage_bytes=event_storage_bytes+v_size
    WHERE id=p_run_id AND user_id=p_user;
    INSERT INTO trellis.run_events(user_id,run_id,sequence,event_type,event_version,data)
    VALUES(p_user,p_run_id,v_run.last_event_sequence+1,p_event_type,p_event_version,p_data)
    RETURNING * INTO v_event;
    RETURN to_jsonb(v_event);
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.append_event(uuid,uuid,text,jsonb,integer)
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;

-- Migration002 enriches completion events after appending them. Those updates
-- must count their byte delta as part of the same RPC transaction.
CREATE FUNCTION trellis_private.account_event_data_update() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
    v_run trellis.runs%ROWTYPE;
    v_size bigint:=octet_length(NEW.data::text);
    v_total bigint;
    v_limit bigint:=CASE WHEN OLD.event_type IN ('run.cancellation_requested','assistant.completed',
        'run.completed','run.failed','run.cancelled','run.interrupted') THEN 16777216 ELSE 16711680 END;
BEGIN
    IF (NEW.user_id,NEW.run_id,NEW.sequence,NEW.event_type,NEW.event_version)
        IS DISTINCT FROM (OLD.user_id,OLD.run_id,OLD.sequence,OLD.event_type,OLD.event_version) THEN
        RAISE EXCEPTION 'event identity is immutable' USING ERRCODE='22023';
    END IF;
    SELECT * INTO v_run FROM trellis.runs WHERE id=NEW.run_id AND user_id=NEW.user_id FOR UPDATE;
    v_total:=v_run.event_storage_bytes-octet_length(OLD.data::text)+v_size;
    IF v_size>262144 OR v_total>v_limit THEN
        RAISE EXCEPTION 'event_storage_limit' USING ERRCODE='22023';
    END IF;
    UPDATE trellis.runs SET event_storage_bytes=v_total WHERE id=NEW.run_id AND user_id=NEW.user_id;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.account_event_data_update()
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
CREATE TRIGGER account_event_data_update BEFORE UPDATE ON trellis.run_events
FOR EACH ROW EXECUTE FUNCTION trellis_private.account_event_data_update();

-- A capped run cannot afford one cleanup event per pending tool. Reconcile the
-- remaining records atomically when its single terminal lifecycle event saves.
CREATE FUNCTION trellis_private.finalize_run_tools() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    UPDATE trellis.tool_calls SET
        status=CASE WHEN NEW.status='failed' THEN 'failed' ELSE 'cancelled' END,
        finished_at=COALESCE(NEW.finished_at,now()),updated_at=now()
    WHERE user_id=NEW.user_id AND run_id=NEW.id AND status IN ('pending','running');
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.finalize_run_tools()
    FROM PUBLIC,anon,authenticated,trellis_catalog_sync;
CREATE TRIGGER finalize_run_tools AFTER UPDATE OF status ON trellis.runs
FOR EACH ROW WHEN(NEW.status IN ('completed','failed','cancelled','interrupted'))
EXECUTE FUNCTION trellis_private.finalize_run_tools();
COMMIT;
