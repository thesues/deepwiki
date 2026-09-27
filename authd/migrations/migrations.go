package migrations

import "embed"

// Files contains the schema used by the authd binary at startup.
//
//go:embed *.sql
var Files embed.FS
