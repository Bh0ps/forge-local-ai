# Framework discovery recipes

Preserve the existing stack and package manager. This guide is evidence to apply to the current project, not an installation directive.

| Existing project | Inspect first | Implementation boundary | Verify |
| --- | --- | --- | --- |
| React/Vite | package.json, lockfile, src entry, vite.config | Existing components, state ownership and CSS tokens | Existing type/build/test scripts and managed preview |
| Next.js | package.json, app/pages routing, server/client boundaries | Keep server secrets off client components; preserve router conventions | Existing lint/type/build and page/API flows |
| Vue/Svelte | package.json, routes, stores and component files | Preserve reactive state and the existing component style | Existing check/build/test scripts |
| Python FastAPI/Flask | pyproject/requirements, routes and service layer | Validation, authorization, scoped persistence and errors | Existing pytest/API fixtures and disposable data |
| Node/Express | package.json, routes, middleware and services | Auth/validation before side effects; existing async error flow | Existing tests and representative endpoint calls |
| Static site | HTML entry, CSS, scripts and asset paths | Semantic markup and working controls without unnecessary backend | Managed static preview and responsive/keyboard checks |

For a net-new UI, use the vetted React/TypeScript/Vite template. Add a backend only when requirements need it. Select versions from the project's lockfile or the vetted template; use primary documentation to verify APIs that may have changed. Dependency installation, server startup and publishing stay under coordinator permissions.
