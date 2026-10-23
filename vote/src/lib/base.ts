// Public base path (Next basePath); available in server and client code.
export const BASE = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
export const withBase = (p: string) => `${BASE}${p}`;
