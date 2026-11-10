package main

import (
	"context"
	"fmt"
	"strings"

	"dagger/malrunar-ci/internal/dagger"
)

type MalrunarCi struct{}

const (
	pythonImage = "python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
	nodeImage   = "node:22.23.3-bookworm-slim@sha256:3ed98443660ff48413b5af19ecefce3b9d50845a901a43420d47cc63a9fddbe9"
)

// Checks runs the repository's isolated Linux unit and syntax suites.
// +check
func (m *MalrunarCi) Checks(ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
) error {
	_, err := m.checksOutput(ctx, source)
	return err
}

// Evaluate each command with its exit code preserved so a failing unittest
// reports its assertion/traceback instead of an opaque Dagger "exit code 1".
func runCheck(ctx context.Context, label string, container *dagger.Container, args []string) (*dagger.Container, string, error) {
	container = container.WithExec(args, dagger.ContainerWithExecOpts{Expect: dagger.ReturnTypeAny})
	code, err := container.ExitCode(ctx)
	if err != nil {
		return nil, "", fmt.Errorf("%s: read exit status: %w", label, err)
	}
	stdout, err := container.Stdout(ctx)
	if err != nil {
		return nil, "", fmt.Errorf("%s: read stdout: %w", label, err)
	}
	stderr, err := container.Stderr(ctx)
	if err != nil {
		return nil, "", fmt.Errorf("%s: read stderr: %w", label, err)
	}
	if code != 0 {
		detail := stdout + "\n" + stderr
		if len(detail) > 12000 {
			detail = detail[len(detail)-12000:]
		}
		return nil, "", fmt.Errorf("%s exited %d:\n%s", label, code, detail)
	}
	return container, stdout, nil
}

func (m *MalrunarCi) checksOutput(ctx context.Context, source *dagger.Directory) (string, error) {
	var out strings.Builder
	nodeTools := dag.Container().From(nodeImage)
	python := dag.Container().From(pythonImage).
		WithExec([]string{"sh", "-ec", "apt-get update && apt-get install -y --no-install-recommends bash git && useradd --uid 10001 --create-home ci && rm -rf /var/lib/apt/lists/*"}).
		WithEnvVariable("PYTHONPATH", "/src/obs-control").
		WithFile("/usr/local/bin/node", nodeTools.File("/usr/local/bin/node")).
		WithDirectory("/src", source).WithWorkdir("/src").
		WithUser("10001:10001").WithEnvVariable("HOME", "/home/ci")
	checks := []struct {
		name string
		args []string
	}{
		// Live pin discovery belongs to the weekly maintenance workflow: PR validation
		// must not depend on the availability of snapshot.ubuntu.com or Mozilla mirrors.
		{"obs-control", []string{"sh", "-ec", "for pattern in test_config.py test_scenes.py test_docs.py; do python3 -m unittest discover -s obs-control/test -p \"$pattern\"; done"}},
		{"registry-and-planner", []string{"python3", "-m", "unittest", "discover", "-s", "scripts/test", "-v"}},
		{"session-kit", []string{"python3", "-m", "unittest", "discover", "-s", "session-kit/test", "-v"}},
		{"thunderbird", []string{"python3", "-I", "-m", "unittest", "discover", "-s", "thunderbird/test", "-v"}},
		{"devops-service", []string{"python3", "-m", "unittest", "discover", "-s", "devops-service/test", "-p", "test_*.py", "-v"}},
		{"browser-launcher", []string{"bash", "work-browser/test/launcher-test.sh"}},
		{"teams-common", []string{"bash", "msteams-mcp/test/unit/common.test.sh"}},
	}
	for _, check := range checks {
		var result string
		var err error
		python, result, err = runCheck(ctx, check.name, python, check.args)
		if err != nil {
			return "", err
		}
		out.WriteString(result)
	}

	node := dag.Container().From(nodeImage).
		WithDirectory("/src", source).WithWorkdir("/src")
	_, result, err := runCheck(ctx, "node-syntax", node, []string{"sh", "-ec", "for file in msteams-mcp/libexec/*.mjs msteams-mcp/patches/*.cjs teams-service/src/*.mjs teams-service/dev/*.mjs; do node --check \"$file\"; done"})
	if err != nil {
		return "", err
	}
	out.WriteString(result)
	return out.String(), nil
}

