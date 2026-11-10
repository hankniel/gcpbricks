import { Link } from "react-router-dom";
import { Breadcrumb, Typography, useDesignSystemTheme } from "@databricks/design-system";

/** Databricks' page head: breadcrumb trail, an icon tile and the title, actions on the right. */
export default function PageTitle({ crumbs = [], icon, title, actions, addOns }: {
  crumbs?: { to: string; label: string }[]; icon?: React.ReactNode; title: React.ReactNode;
  actions?: React.ReactNode; addOns?: React.ReactNode;
}) {
  const { theme } = useDesignSystemTheme();
  return (
    <>
      {crumbs.length > 0 && (
        <Breadcrumb>{crumbs.map((c) => <Breadcrumb.Item key={c.to}><Link to={c.to}>{c.label}</Link></Breadcrumb.Item>)}</Breadcrumb>
      )}
      <div css={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: theme.spacing.md,
                  marginTop: crumbs.length ? 6 : 0, minHeight: 32 }}>
        <div css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, minWidth: 0 }}>
          {icon && <span css={{ width: 32, height: 32, flex: "none", display: "grid", placeItems: "center",
                                borderRadius: theme.borders.borderRadiusMd, background: theme.colors.backgroundSecondary }}>{icon}</span>}
          <Typography.Title level={2} css={{ "&&": { margin: 0 } }}>{title}</Typography.Title>
          {addOns}
        </div>
        {actions && <div css={{ display: "flex", gap: theme.spacing.sm, flex: "none" }}>{actions}</div>}
      </div>
    </>
  );
}
