BEGIN;

CREATE OR REPLACE FUNCTION trellis_private.attach_research_mode() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    NEW.run_mode:=COALESCE(NULLIF(current_setting('trellis.run_mode',true),''),'chat');
    IF NEW.run_mode='research' THEN
        NEW.max_model_calls:=CASE WHEN NEW.budget_preset='longer' THEN 120 ELSE 72 END;
        NEW.max_tool_calls:=CASE WHEN NEW.budget_preset='longer' THEN 384 ELSE 192 END;
        NEW.max_total_tokens:=CASE WHEN NEW.budget_preset='longer' THEN 600000 ELSE 300000 END;
        NEW.max_cost_usd:=CASE WHEN NEW.budget_preset='longer' THEN 15 ELSE 6 END;
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION trellis_private.attach_research_mode() FROM PUBLIC,anon,authenticated;

ALTER TABLE trellis.research_reservations DROP CONSTRAINT research_reservations_reserved_cost_check;
ALTER TABLE trellis.research_reservations ADD CONSTRAINT research_reservations_reserved_cost_check
    CHECK (reserved_cost>=0 AND reserved_cost<='15'::numeric);

COMMIT;
