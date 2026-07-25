declare const __S2S_FRONTEND_REPOSITORY_COMMIT__: string | null;
declare const __S2S_FRONTEND_REPOSITORY_DIRTY__: boolean | null;

export const FRONTEND_BUILD_PROVENANCE = Object.freeze({
  commit: typeof __S2S_FRONTEND_REPOSITORY_COMMIT__ === 'undefined'
    ? null
    : __S2S_FRONTEND_REPOSITORY_COMMIT__,
  dirty: typeof __S2S_FRONTEND_REPOSITORY_DIRTY__ === 'undefined'
    ? null
    : __S2S_FRONTEND_REPOSITORY_DIRTY__,
});
