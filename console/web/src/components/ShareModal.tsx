import { useEffect, useState } from "react";
import {
  Alert, Button, CloseSmallIcon, FormUI, Input, Modal, Radio, SimpleSelect, SimpleSelectOption, Spacer, TableSkeleton,
  Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { notebooksApi, type NotebookShare } from "../api";

type Level = "view" | "edit";
const EMAIL = /^[^@\s/]+@[^@\s/]+\.[^@\s/]+$/;

/** A Users/ notebook's sharing, as Databricks' Share dialog: people added as Can view or Can
 *  edit, and whether everyone else in the console views it. */
export default function ShareModal({ path, open, onClose }: { path: string; open: boolean; onClose: () => void }) {
  const { theme } = useDesignSystemTheme();
  const [share, setShare] = useState<NotebookShare | null>(null);
  const [email, setEmail] = useState("");
  const [level, setLevel] = useState<Level>("view");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setShare(null); setError(null); setEmail("");
    notebooksApi.share(path).then(setShare, (e: Error) => setError(e.message));
  }, [open, path]);

  const who = email.trim().toLowerCase();
  const add = () => {
    if (!share || !EMAIL.test(who) || who === share.owner) return;
    setShare({ ...share, people: { ...share.people, [who]: level } });
    setEmail("");
  };
  const setPerson = (person: string, to: Level | null) => {
    if (!share) return;
    const { [person]: _, ...rest } = share.people;
    setShare({ ...share, people: to ? { ...share.people, [person]: to } : rest });
  };
  const save = async () => {
    if (!share) return;
    setBusy(true); setError(null);
    try {
      await notebooksApi.setShare(path, { general: share.general, people: share.people });
      onClose();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };

  const row = { display: "flex", alignItems: "center", gap: theme.spacing.sm, padding: `${theme.spacing.xs}px 0` } as const;
  return (
    <Modal componentId="nb.share.modal" title={`Share ${path.split("/").pop()}`} visible={open} onCancel={onClose}
           okText="Save" cancelText="Cancel" onOk={save} confirmLoading={busy} okButtonProps={{ disabled: !share }}>
      {error && <><Alert componentId="nb.share.error" type="error" message={error} closable={false} /><Spacer size="sm" /></>}
      {!share && !error && <TableSkeleton lines={4} />}
      {share && <>
        <FormUI.Label htmlFor="nb-share-email">Add people</FormUI.Label>
        <div css={row}>
          <Input id="nb-share-email" componentId="nb.share.email" placeholder="user@example.com" value={email}
                 onChange={(e) => setEmail(e.target.value)} onPressEnter={add} css={{ flex: 1 }} />
          <SimpleSelect componentId="nb.share.level" id="nb-share-level" value={level} onChange={(e) => setLevel(e.target.value as Level)}>
            <SimpleSelectOption value="view">Can view</SimpleSelectOption>
            <SimpleSelectOption value="edit">Can edit</SimpleSelectOption>
          </SimpleSelect>
          <Button componentId="nb.share.add" onClick={add} disabled={!EMAIL.test(who) || who === share.owner}>Add</Button>
        </div>
        <Spacer size="md" />
        <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>People with access</Typography.Title>
        <div css={row}>
          <span css={{ flex: 1 }}>{share.owner} <Typography.Text color="secondary">(owner)</Typography.Text></span>
          <Typography.Text color="secondary">Can manage</Typography.Text>
        </div>
        {Object.entries(share.people).map(([person, lvl]) => (
          <div key={person} css={row}>
            <span css={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{person}</span>
            <SimpleSelect componentId="nb.share.person.level" id={`nb-share-${person}`} value={lvl}
                          onChange={(e) => setPerson(person, e.target.value as Level)}>
              <SimpleSelectOption value="view">Can view</SimpleSelectOption>
              <SimpleSelectOption value="edit">Can edit</SimpleSelectOption>
            </SimpleSelect>
            <Button componentId="nb.share.person.remove" icon={<CloseSmallIcon />} aria-label={`Remove ${person}`}
                    onClick={() => setPerson(person, null)} />
          </div>
        ))}
        <Spacer size="md" />
        <Typography.Title level={4} css={{ "&&": { margin: 0 } }}>General access</Typography.Title>
        <Spacer size="xs" />
        <Radio.Group componentId="nb.share.general" name="nb-share-general" value={share.general}
                     onChange={(e) => setShare({ ...share, general: e.target.value as NotebookShare["general"] })}>
          <Radio value="view">Everyone in the console can view</Radio>
          <Radio value="private">Only the people added</Radio>
        </Radio.Group>
        <FormUI.Hint>The console's admins can always open it. Viewers see its cells and saved outputs, but don't edit or run it.</FormUI.Hint>
      </>}
    </Modal>
  );
}
