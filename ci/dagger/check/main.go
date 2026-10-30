package main

import (
	"context"
	"fmt"
	"strings"

	"dagger/check/internal/dagger"
)

type Malrunar struct{}

const (
	pythonImage = "python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
	nodeImage   = "node:22.23.3-bookworm-slim@sha256:3ed98443660ff48413b5af19ecefce3b9d50845a901a43420d47cc63a9fddbe9"
)

// Checks runs the repository's isolated Linux unit and syntax suites.
func (m *Malrunar) Checks(ctx context.Context, source *dagger.Directory) (string, error) {
	var out strings.Builder
	nodeTools := dag.Container().From(nodeImage)
	python := dag.Container().From(pythonImage).
		WithExec([]string{"sh", "-ec", "apt-get update && apt-get install -y --no-install-recommends bash git && rm -rf /var/lib/apt/lists/*"}).
		WithFile("/usr/local/bin/node", nodeTools.File("/usr/local/bin/node")).
		WithDirectory("/src", source).WithWorkdir("/src").
		WithExec([]string{"sh", "-ec", "for pattern in test_config.py test_scenes.py test_docs.py; do python3 -m unittest discover -s obs-control/test -p \"$pattern\"; done"}).
		WithExec([]string{"python3", "-m", "unittest", "discover", "-s", "scripts/test", "-v"}).
		WithExec([]string{"python3", "-m", "unittest", "discover", "-s", "session-kit/test", "-v"}).
		WithExec([]string{"python3", "-I", "-m", "unittest", "discover", "-s", "thunderbird/test", "-v"}).
		WithExec([]string{"python3", "-m", "unittest", "discover", "-s", "devops-service/test", "-p", "test_*.py", "-v"}).
		WithExec([]string{"python3", "-I", "scripts/bump_pins.py", "--dry-run"}).
		WithExec([]string{"bash", "work-browser/test/launcher-test.sh"}).
		WithExec([]string{"bash", "msteams-mcp/test/unit/common.test.sh"})
	text, err := python.Stdout(ctx)
	if err != nil {
		return "", fmt.Errorf("Python and shell unit checks: %w", err)
	}
	out.WriteString(text)

	node := dag.Container().From(nodeImage).
		WithDirectory("/src", source).WithWorkdir("/src").
		WithExec([]string{"sh", "-ec", "for file in msteams-mcp/libexec/*.mjs msteams-mcp/patches/*.cjs teams-service/src/*.mjs teams-service/dev/*.mjs; do node --check \"$file\"; done"})
	text, err = node.Stdout(ctx)
	if err != nil {
		return "", fmt.Errorf("Node syntax checks: %w", err)
	}
	out.WriteString(text)
	return out.String(), nil
}
