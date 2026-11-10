import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  Alert, Breadcrumb, Button, SparkleDoubleIcon, DropdownMenu, Empty, FolderFillIcon, FormUI, HomeIcon, Input, Modal,
  NotebookIcon, OverflowIcon, Spacer, TableCell, TableRow, TableSkeleton, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { Table, TableHeader } from "../components/Table";
import { api, notebooksApi } from "../api";
import ClaudeSetup from "../components/ClaudeSetup";
import PageTitle from "../components/PageTitle";
import { ago, useLoad, when } from "../util";

const WORKSPACE_ROOT = "/Workspace/";

/** Databricks' Workspace browser over the console's notebooks: Users/<email>/ and Shared/. */
export default function WorkspacePage() {
  const { theme } = useDesignSystemTheme();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const path = params.get("path") ?? "";
  const me = useLoad(api.me, []);
  const { data, error, reload } = useLoad(() => notebooksApi.folder(path), [path]);
  const [ask, setAsk] = useState<"notebook" | "folder" | null>(null);
  const [name, setName] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [claude, setClaude] = useState(false);
  const go = (p: string) => setParams(p ? { path: p } : {});
  const writable = !!me.data && (me.data.admin || path === "Shared" || path.startsWith("Shared/")
    || path === `Users/${me.data.email}` || path.startsWith(`Users/${me.data.email}/`));
  const crumbs = path ? path.split("/") : [];

  const create = async () => {
    const clean = name.trim().replace(/^\/+|\/+$/g, "");
    if (!clean) return;
    try {
      if (ask === "notebook") {
        const r = await notebooksApi.create(`${path}/${clean}`);
        nav(`/workspace/notebook?path=${encodeURIComponent(r.path)}`);
      } else {
        await notebooksApi.mkdir(`${path}/${clean}`);
        setAsk(null); reload();
      }
    } catch (e) { setMsg((e as Error).message); }
  };
  const remove = async (p: string) => {
    try { await notebooksApi.remove(p); reload(); } catch (e) { setMsg((e as Error).message); }
  };

  const home = data?.home;
  const atHome = !!home && (path === home || path.startsWith(`${home}/`));
  const place = (on: boolean) => ({
    display: "flex", alignItems: "center", gap: 4, height: 28, padding: "0 8px 0 4px", cursor: "pointer",
    background: on ? (theme.isDarkMode ? "#04355D" : theme.colors.actionDefaultBackgroundPress) : undefined,
    "&:hover": on ? undefined : { background: theme.colors.actionDefaultBackgroundHover },
    "& > span": { display: "flex", alignItems: "center", gap: 4, padding: "0 8px", width: "100%" },
  } as const);

  return (
    // Databricks' workspace browser: the places on the left, the open folder beside them.
    <div css={{ display: "grid", gridTemplateColumns: "350px minmax(0, 1fr)", margin: -theme.spacing.md,
                height: `calc(100% + ${2 * theme.spacing.md}px)` }}>
      <aside css={{ borderRight: `1px solid ${theme.colors.border}`, minHeight: 0, overflow: "auto" }}>
        <div css={{ padding: `${theme.spacing.sm}px ${theme.spacing.sm}px`, lineHeight: "20px" }}><Typography.Text bold>Workspace</Typography.Text></div>
        <div role="button" tabIndex={0} css={place(atHome)} onClick={() => home && go(home)}
             onKeyDown={(e) => e.key === "Enter" && home && go(home)}>
          <span><span css={{ width: 16 }} /><HomeIcon css={{ color: theme.colors.textSecondary }} />Home</span>
        </div>
        <div role="button" tabIndex={0} css={place(!atHome)} onClick={() => go("")} onKeyDown={(e) => e.key === "Enter" && go("")}>
          <span><span css={{ width: 16 }} /><FolderFillIcon css={{ color: theme.colors.actionLinkDefault }} />Workspace</span>
        </div>
      </aside>
      <div css={{ minWidth: 0, overflow: "auto", padding: `${theme.spacing.sm}px ${theme.spacing.md}px ${theme.spacing.md}px` }}>
        <Breadcrumb>
          <Breadcrumb.Item><Typography.Link componentId="ws.crumb.root" onClick={() => go("")}>Workspace</Typography.Link></Breadcrumb.Item>
          {crumbs.slice(0, -1).map((c, i) => (
            <Breadcrumb.Item key={i}><Typography.Link componentId="ws.crumb" onClick={() => go(crumbs.slice(0, i + 1).join("/"))}>{c}</Typography.Link></Breadcrumb.Item>
          ))}
          <Breadcrumb.Item>{""}</Breadcrumb.Item>
        </Breadcrumb>
        <div css={{ marginTop: 6 }}>
          <PageTitle title={crumbs[crumbs.length - 1] ?? "Workspace"} actions={
            <div css={{ display: "flex", gap: theme.spacing.sm }}>
              <Button componentId="ws.claude" icon={<SparkleDoubleIcon />} onClick={() => setClaude(true)}>Use with Claude</Button>
              <Button componentId="ws.newfolder" disabled={!writable} onClick={() => { setName(""); setAsk("folder"); }}>New folder</Button>
              <Button componentId="ws.newnb" type="primary" disabled={!writable} onClick={() => { setName(""); setAsk("notebook"); }}>New notebook</Button>
            </div>} />
        </div>
        <ClaudeSetup open={claude} onClose={() => setClaude(false)} />
        <Spacer size="md" />
        {msg && <><Alert componentId="ws.msg" type="error" message={msg} onClose={() => setMsg(null)} /><Spacer size="sm" /></>}
        {error && <Alert componentId="ws.error" type="error" message="Couldn't read the workspace" description={error} closable={false} />}
        {!data && !error && <TableSkeleton lines={6} />}
        {data && (
          <Table scrollable empty={data.folders.length + data.notebooks.length ? undefined
            : <Empty title="Empty folder" description={writable ? "Create a notebook or a folder here." : "Nothing here yet."} />}>
            <TableRow isHeader>
              <TableHeader componentId="ws.h.name">Name</TableHeader>
              <TableHeader componentId="ws.h.type" css={{ flex: "0 0 120px" }}>Type</TableHeader>
              <TableHeader componentId="ws.h.modified" css={{ flex: "0 0 200px" }}>Last modified</TableHeader>
              <TableHeader componentId="ws.h.actions" css={{ flex: "0 0 48px" }}> </TableHeader>
            </TableRow>
            {data.folders.map((f) => (
              <TableRow key={f.path}>
                <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", alignItems: "center", gap: theme.spacing.sm }}>
                  <FolderFillIcon css={{ color: theme.colors.actionLinkDefault }} />
                  <Typography.Link componentId="ws.folder" onClick={() => go(f.path)}>{f.name}</Typography.Link></span></TableCell>
                <TableCell css={{ flex: "0 0 120px" }}>Folder</TableCell>
                <TableCell css={{ flex: "0 0 200px" }}>-</TableCell>
                <TableCell css={{ flex: "0 0 48px" }} />
              </TableRow>
            ))}
            {data.notebooks.map((n) => (
              <TableRow key={n.path}>
                <TableCell><span css={{ display: "inline-flex", verticalAlign: "top", alignItems: "center", gap: theme.spacing.sm }}>
                  <NotebookIcon css={{ color: theme.colors.textSecondary }} />
                  <Link to={`/workspace/notebook?path=${encodeURIComponent(n.path)}`}>{n.name.replace(/\.ipynb$/, "")}</Link></span></TableCell>
                <TableCell css={{ flex: "0 0 120px" }}>Notebook</TableCell>
                <TableCell css={{ flex: "0 0 200px" }}><span title={when(n.updated, true)}>{ago(n.updated)}</span></TableCell>
                <TableCell css={{ flex: "0 0 48px" }}>
                  <DropdownMenu.Root>
                    <DropdownMenu.Trigger asChild><Button componentId="ws.nb.more" size="small" icon={<OverflowIcon />} aria-label={`Actions for ${n.name}`} /></DropdownMenu.Trigger>
                    <DropdownMenu.Content align="end">
                      <DropdownMenu.Item componentId="ws.nb.copy" onClick={() => navigator.clipboard.writeText(WORKSPACE_ROOT + n.path)}>
                        Copy path for a notebook_task</DropdownMenu.Item>
                      <DropdownMenu.Item componentId="ws.nb.delete" disabled={!writable} onClick={() => remove(n.path)}>Delete</DropdownMenu.Item>
                    </DropdownMenu.Content>
                  </DropdownMenu.Root>
                </TableCell>
              </TableRow>
            ))}
          </Table>
        )}
      </div>
      <Modal componentId="ws.new" title={ask === "notebook" ? "New notebook" : "New folder"} visible={!!ask} okText="Create"
             cancelText="Cancel" onCancel={() => setAsk(null)} onOk={create}>
        <FormUI.Label htmlFor="ws-name">Name, in {path || "the workspace"}</FormUI.Label>
        <Input id="ws-name" componentId="ws.new.name" value={name} onChange={(e) => setName(e.target.value)}
               placeholder={ask === "notebook" ? "analysis" : "experiments"} onPressEnter={create} />
      </Modal>
    </div>
  );
}
