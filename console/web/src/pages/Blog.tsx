import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { StateEffect } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import {
  Alert, BookIcon, Button, DangerModal, Empty, FormUI, Input, Modal, ParagraphSkeleton, PencilIcon, PlusIcon, SearchIcon,
  Spacer, Tag, TrashIcon, Typography, useDesignSystemTheme,
} from "@databricks/design-system";
import { blogApi, type Post } from "../api";
import CodeEditor from "../components/CodeEditor";
import PageTitle from "../components/PageTitle";
import RichMarkdown, { renderMarkdown } from "../components/RichMarkdown";
import SidePanel, { type PanelKind } from "../components/SidePanel";
import { useToast } from "../components/Toast";
import { useLoad, when } from "../util";

const byline = (p: { author: string; updated: string }) => `${p.author} · ${when(p.updated, true)}`;

export function BlogPage() {
  const { theme } = useDesignSystemTheme();
  const nav = useNavigate();
  const { data, error } = useLoad(blogApi.posts, []);
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [title, setTitle] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const tags = useMemo(() => [...new Set((data ?? []).flatMap((p) => p.tags))].sort(), [data]);
  const shown = (data ?? []).filter((p) => (!tag || p.tags.includes(tag))
    && `${p.title} ${p.summary} ${p.tags.join(" ")} ${p.author}`.toLowerCase().includes(search.toLowerCase()));
  const create = () => blogApi.create(title).then((r) => nav(`/blog/${r.slug}/edit`), (e: Error) => setCreateError(e.message));
  return (
    <>
      <PageTitle title="Blog" actions={
        <Button componentId="blog.new" type="primary" icon={<PlusIcon />} onClick={() => { setTitle(""); setCreateError(null); setAsking(true); }}>
          New post</Button>} />
      <Spacer size="md" />
      <div css={{ display: "flex", gap: theme.spacing.sm, flexWrap: "wrap", alignItems: "center" }}>
        <Input componentId="blog.search" prefix={<SearchIcon />} placeholder="Search posts" value={search}
               onChange={(e) => setSearch(e.target.value)} css={{ maxWidth: 320 }} allowClear />
        {tags.map((t) => (
          <Tag key={t} componentId="blog.tag" color={tag === t ? "indigo" : undefined} onClick={() => setTag(tag === t ? null : t)}
               css={{ cursor: "pointer" }}>{t}</Tag>
        ))}
      </div>
      <Spacer size="md" />
      {error && <Alert componentId="blog.error" type="error" message="Couldn't load the blog" description={error} closable={false} />}
      {!data && !error && <ParagraphSkeleton label="Loading posts" />}
      {data && !shown.length && <Empty title={data.length ? "No posts match" : "No posts yet"}
                                       description={data.length ? "Try another search or tag." : "Write the first guide with New post."} />}
      <div css={{ display: "flex", flexDirection: "column", maxWidth: 880 }}>
        {shown.map((p) => (
          <article key={p.slug} css={{ padding: `${theme.spacing.md}px 0`, borderBottom: `1px solid ${theme.colors.border}` }}>
            <Link to={`/blog/${p.slug}`} css={{ fontSize: 18, fontWeight: 600 }}>{p.title}</Link>
            {p.summary && <Typography.Paragraph color="secondary" css={{ "&&": { margin: "4px 0 0" } }}>{p.summary}</Typography.Paragraph>}
            <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.xs, marginTop: theme.spacing.xs, flexWrap: "wrap" }}>
              <Typography.Text size="sm" color="secondary">{byline(p)}</Typography.Text>
              {p.tags.map((t) => <Tag key={t} componentId="blog.post.tag">{t}</Tag>)}
            </div>
          </article>
        ))}
      </div>
      <Modal componentId="blog.new.modal" title="New post" visible={asking} okText="Create" cancelText="Cancel"
             onCancel={() => setAsking(false)} onOk={create} okButtonProps={{ disabled: !title.trim() }}>
        <FormUI.Label htmlFor="blog-title">Title</FormUI.Label>
        <Input id="blog-title" componentId="blog.new.title" value={title} onChange={(e) => setTitle(e.target.value)}
               placeholder="Deploying a bundle, step by step" onPressEnter={() => title.trim() && create()} autoFocus />
        {createError && <FormUI.Message type="error" message={createError} />}
      </Modal>
    </>
  );
}

const CONTENTS: PanelKind = { key: "mlops-console.blog-contents", name: "contents", width: 260, min: 180, max: 480, sticky: true };

/** Where a post's headings are, to jump to: h2 and h3 only, as GitHub's outline. */
function Contents({ headings }: { headings: { id: string; level: number; text: string }[] }) {
  const { theme } = useDesignSystemTheme();
  const shown = headings.filter((h) => h.level === 2 || h.level === 3);
  if (shown.length < 2) return null;
  return (
    <nav aria-label="Contents" css={{ fontSize: 13, maxHeight: "100vh", overflowY: "auto", paddingRight: theme.spacing.lg * 2 }}>
      <Typography.Text bold size="sm">Contents</Typography.Text>
      {shown.map((h) => (
        <a key={h.id} href={`#${h.id}`} css={{ display: "block", padding: "3px 0", paddingLeft: h.level === 3 ? 12 : 0,
                                              "&&": { color: theme.colors.textSecondary } }}
           onClick={(e) => { e.preventDefault(); document.getElementById(h.id)?.scrollIntoView({ behavior: "smooth" }); }}
           dangerouslySetInnerHTML={{ __html: h.text }} />
      ))}
    </nav>
  );
}

export function BlogPostPage() {
  const { slug = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const nav = useNavigate();
  const toast = useToast();
  const { data, error } = useLoad(() => blogApi.post(slug), [slug]);
  const [deleting, setDeleting] = useState(false);
  const rendered = useMemo(() => renderMarkdown(data?.body ?? ""), [data?.body]);
  return (
    <>
      <PageTitle crumbs={[{ to: "/blog", label: "Blog" }]} icon={<BookIcon />} title={data?.title ?? slug}
        actions={data?.can_edit && <>
          <Button componentId="blog.edit" icon={<PencilIcon />} onClick={() => nav(`/blog/${slug}/edit`)}>Edit</Button>
          <Button componentId="blog.delete" icon={<TrashIcon />} danger onClick={() => setDeleting(true)}>Delete</Button>
        </>} />
      {data && (
        <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.xs, marginTop: theme.spacing.xs, flexWrap: "wrap" }}>
          <Typography.Text size="sm" color="secondary">{byline(data)}</Typography.Text>
          {data.tags.map((t) => <Tag key={t} componentId="blog.post.tag">{t}</Tag>)}
        </div>
      )}
      <Spacer size="lg" />
      {error && <Alert componentId="blog.post.error" type="error" message="Couldn't load the post" description={error} closable={false} />}
      {!data && !error && <ParagraphSkeleton label="Loading the post" />}
      {data && (
        <SidePanel kind={CONTENTS}>
          {data.body.trim() ? <RichMarkdown html={rendered.html} />
            : <Empty title="Nothing here yet" description={data.can_edit ? "Edit the post to write it." : "Its author hasn't written it yet."} />}
          <Contents headings={rendered.headings} />
        </SidePanel>
      )}
      <DangerModal componentId="blog.delete.modal" visible={deleting} title={`Delete ${data?.title ?? slug}?`}
                   okText="Delete post" cancelText="Cancel" onCancel={() => setDeleting(false)}
                   onOk={() => {
                     setDeleting(false);
                     blogApi.remove(slug).then(() => { toast({ title: `Deleted ${data?.title ?? slug}` }); nav("/blog"); },
                                               (e: Error) => toast({ title: "Couldn't delete it", description: e.message, severity: "error" }));
                   }}>
        The post and the images uploaded to it go for good.
      </DangerModal>
    </>
  );
}

const IMAGE = /^image\/(png|jpeg|gif|webp)$/;

export function BlogEditorPage() {
  const { slug = "" } = useParams();
  const { theme } = useDesignSystemTheme();
  const nav = useNavigate();
  const toast = useToast();
  const loaded = useLoad(() => blogApi.post(slug), [slug]);
  const [post, setPost] = useState<Post | null>(null);
  const [tags, setTags] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(0);
  const [preview, setPreview] = useState("");
  const pane = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!loaded.data) return;
    setPost(loaded.data);
    setTags(loaded.data.tags.join(", "));
    setPreview(loaded.data.body);
    setDirty(false);
  }, [loaded.data]);

  // The preview follows the text once typing pauses: mermaid redraws every diagram on each render.
  useEffect(() => {
    const t = setTimeout(() => setPreview(post?.body ?? ""), 300);
    return () => clearTimeout(t);
  }, [post?.body]);
  const rendered = useMemo(() => renderMarkdown(preview), [preview]);

  const edit = (change: Partial<Post>) => { setPost((p) => p && { ...p, ...change }); setDirty(true); };
  const save = () => {
    if (!post || saving) return;
    setSaving(true);
    blogApi.save(slug, { title: post.title, tags: tags.split(",").map((t) => t.trim()).filter(Boolean), summary: post.summary,
                         body: post.body, generation: post.generation })
      .then((r) => { setPost((p) => p && { ...p, generation: r.generation }); setDirty(false); toast({ title: "Saved" }); },
            (e: Error) => toast({ title: "Couldn't save", description: e.message, severity: "error" }))
      .finally(() => setSaving(false));
  };
  const saveRef = useRef(save);
  saveRef.current = save;

  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); saveRef.current(); }
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, []);
  useEffect(() => {
    if (!dirty) return;
    const leave = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", leave);
    return () => window.removeEventListener("beforeunload", leave);
  }, [dirty]);

  const view = () => {
    const el = pane.current?.querySelector<HTMLElement>(".cm-editor");
    return el ? EditorView.findFromDOM(el) : null;
  };
  // Prose wraps; the notebook's editor, built for code, doesn't.
  useEffect(() => {
    view()?.dispatch({ effects: StateEffect.appendConfig.of(EditorView.lineWrapping) });
  }, [post !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  const upload = (files: File[]) => {
    const images = files.filter((f) => IMAGE.test(f.type));
    if (!images.length) return false;
    for (const f of images) {
      setUploading((n) => n + 1);
      blogApi.upload(slug, f).then(
        ({ url }) => {
          const v = view();
          const text = `![${f.name.replace(/\.[^.]+$/, "")}](${url})\n`;
          if (v) v.dispatch(v.state.replaceSelection(text));
          else edit({ body: `${post?.body ?? ""}\n${text}` });
        },
        (e: Error) => toast({ title: `Couldn't upload ${f.name}`, description: e.message, severity: "error" }),
      ).finally(() => setUploading((n) => n - 1));
    }
    return true;
  };

  if (loaded.error) return <Alert componentId="blog.editor.error" type="error" message="Couldn't load the post" description={loaded.error} closable={false} />;
  if (!post) return <ParagraphSkeleton label="Loading the post" />;
  if (!post.can_edit) {
    return <Alert componentId="blog.editor.denied" type="warning" message="You can't edit this post"
                  description={<>Its author ({post.author}) and the console's admins edit it. <Link to={`/blog/${slug}`}>Read it</Link></>}
                  closable={false} />;
  }
  return (
    <div css={{ display: "flex", flexDirection: "column", height: "100%", gap: theme.spacing.sm }}>
      <PageTitle crumbs={[{ to: "/blog", label: "Blog" }, { to: `/blog/${slug}`, label: post.title }]} icon={<PencilIcon />}
        title="Edit post"
        addOns={<Typography.Text size="sm" color="secondary">
          {uploading ? "Uploading image..." : saving ? "Saving..." : dirty ? "Unsaved changes" : "Saved"}</Typography.Text>}
        actions={<>
          <Button componentId="blog.editor.done" onClick={() => nav(`/blog/${slug}`)}>{dirty ? "Discard" : "Done"}</Button>
          <Button componentId="blog.editor.save" type="primary" loading={saving} disabled={!dirty || !post.title.trim()} onClick={save}>Save</Button>
        </>} />
      <div css={{ display: "grid", gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr) minmax(0, 2fr)", gap: theme.spacing.sm }}>
        <Input componentId="blog.editor.title" aria-label="Title" placeholder="Title" value={post.title}
               onChange={(e) => edit({ title: e.target.value })} />
        <Input componentId="blog.editor.tags" aria-label="Tags" placeholder="Tags, comma-separated" value={tags}
               onChange={(e) => { setTags(e.target.value); setDirty(true); }} />
        <Input componentId="blog.editor.summary" aria-label="Summary" placeholder="One-line summary, shown in the list" value={post.summary}
               onChange={(e) => edit({ summary: e.target.value })} />
      </div>
      <div css={{ flex: 1, minHeight: 0, display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                  border: `1px solid ${theme.colors.border}`, borderRadius: theme.borders.borderRadiusMd }}>
        <div ref={pane} css={{ overflow: "auto", padding: theme.spacing.sm, borderRight: `1px solid ${theme.colors.border}` }}
             onPasteCapture={(e) => { if (upload([...e.clipboardData.files])) { e.preventDefault(); e.stopPropagation(); } }}
             onDropCapture={(e) => { if (upload([...e.dataTransfer.files])) { e.preventDefault(); e.stopPropagation(); } }}>
          <CodeEditor value={post.body} onChange={(body) => edit({ body })} language="markdown" numbers autoFocus />
        </div>
        <div css={{ overflow: "auto", padding: theme.spacing.md }}>
          {preview.trim() ? <RichMarkdown html={rendered.html} />
            : <Typography.Text color="secondary">
                Markdown, with ```mermaid diagrams, $maths$, &gt; [!NOTE] callouts and code in any language. Paste or drop an image to upload it.
              </Typography.Text>}
        </div>
      </div>
    </div>
  );
}
