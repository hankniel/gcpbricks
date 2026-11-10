import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Button, FormUI, Input, Modal, Spacer, Tag, ToggleButton, Typography } from "@databricks/design-system";
import { api } from "../api";
import { useLoad } from "../util";

export function RetiredTag({ retired }: { retired?: string | null }) {
  return retired ? <Tag componentId="retired.tag" color="lemon" title={`Retired ${retired}`}>Retired</Tag> : null;
}

/** A list's "Show retired" switch: retired rows stay hidden until it is on. */
export function useShowRetired(componentId: string) {
  const [show, setShow] = useState(false);
  const toggle = <ToggleButton componentId={componentId} pressed={show} onPressedChange={setShow}>Show retired</ToggleButton>;
  return { keep: (r: { retired?: string | null }) => show || !r.retired, toggle };
}

/** A retired resource's banner; an admin may delete it, after typing its name. */
export function RetiredNotice({ kind, name, retired, remove, back }: {
  kind: string; name: string; retired?: string | null; remove: () => Promise<unknown>; back: string;
}) {
  const nav = useNavigate();
  const me = useLoad(api.me, []);
  const [asking, setAsking] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!retired) return null;
  const confirm = async () => {
    setBusy(true);
    try { await remove(); nav(back); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <>
      <Alert componentId="retired.notice" type="warning" closable={false}
        message={`Retired ${retired}: its bundle no longer declares this ${kind}`}
        description={me.data?.admin ? (
          <Button componentId="retired.delete" danger size="small" onClick={() => { setTyped(""); setError(null); setAsking(true); }}>
            Delete {kind}</Button>) : "Only a console admin can delete it."} />
      <Spacer size="md" />
      <Modal componentId="retired.modal" title={`Delete ${kind} ${name}`} visible={asking} okText="Delete"
             okButtonProps={{ danger: true, disabled: typed !== name }} cancelText="Cancel" confirmLoading={busy}
             onCancel={() => setAsking(false)} onOk={confirm}>
        <Typography.Paragraph>This can't be undone.</Typography.Paragraph>
        <FormUI.Label htmlFor="retired-name">Type <Typography.Text code>{name}</Typography.Text> to confirm</FormUI.Label>
        <Input id="retired-name" componentId="retired.name" value={typed} onChange={(e) => setTyped(e.target.value)} />
        {error && <><Spacer size="sm" /><Alert componentId="retired.error" type="error" message={error} closable={false} /></>}
      </Modal>
    </>
  );
}
