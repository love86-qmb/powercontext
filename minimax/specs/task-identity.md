# Stable repository identity

The SDD task ledger must use one stable key for the same checkout spelling on
Windows or POSIX. `canonical_repository_key(value)` is governed by these rules:

1. Reject an empty or whitespace-only value with `ValueError`.
2. Convert backslashes to forward slashes.
3. Collapse repeated separators, except that a leading UNC `//` remains `//`.
4. Remove trailing separators unless the value is `/` or a drive root such as `c:/`.
5. Normalize an ASCII Windows drive letter and all other ASCII path letters to lowercase.
6. Do not access the filesystem. The result depends only on the supplied text.

Examples:

- `C:\\Work\\PowerContext\\` → `c:/work/powercontext`
- `c:/work//powercontext` → `c:/work/powercontext`
- `//SERVER/Share/Repo/` → `//server/share/repo`
- `/srv/PowerContext/` → `/srv/powercontext`

The immutable regression tests define the accepted boundary behavior.
