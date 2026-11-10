declare module "*.css";
// Vite hands back an asset's URL for an import ending in ?url.
declare module "*?url" { const url: string; export default url; }
