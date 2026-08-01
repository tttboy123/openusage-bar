package openusageplugin

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/usage"
)

const fixtureScope = "anon_0123456789abcdef"

type fixtureDocument struct {
	UpstreamVersion              string `json:"upstreamVersion"`
	TokenAccountingSchemaVersion int    `json:"tokenAccountingSchemaVersion"`
	Record                       struct {
		Provider        string               `json:"provider"`
		Model           string               `json:"model"`
		RequestedAt     string               `json:"requestedAt"`
		LatencyMS       int64                `json:"latencyMs"`
		TTFTMS          int64                `json:"ttftMs"`
		Failed          bool                 `json:"failed"`
		TokenBreakdown  usage.TokenBreakdown `json:"tokenBreakdown"`
		APIKey          string               `json:"apiKey"`
		AuthID          string               `json:"authId"`
		AuthType        string               `json:"authType"`
		Alias           string               `json:"alias"`
		Source          string               `json:"source"`
		FailureBody     string               `json:"failureBody"`
		ResponseHeaders map[string]string    `json:"responseHeaders"`
	} `json:"record"`
}

func loadFixture(t *testing.T) fixtureDocument {
	t.Helper()
	encoded, err := os.ReadFile(filepath.Join("testdata", "cliproxyapi-v7.2.113-usage-v2.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture fixtureDocument
	if err := json.Unmarshal(encoded, &fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.UpstreamVersion != "v7.2.113" || fixture.TokenAccountingSchemaVersion != usage.TokenAccountingSchemaVersion {
		t.Fatal("fixture version drift")
	}
	return fixture
}

func fixtureRecord(t *testing.T) (usage.Record, []string) {
	t.Helper()
	fixture := loadFixture(t)
	requestedAt, err := time.Parse(time.RFC3339Nano, fixture.Record.RequestedAt)
	if err != nil {
		t.Fatal(err)
	}
	headers := make(http.Header)
	private := []string{
		fixture.Record.APIKey,
		fixture.Record.AuthID,
		fixture.Record.AuthType,
		fixture.Record.Alias,
		fixture.Record.Source,
		fixture.Record.FailureBody,
	}
	for key, value := range fixture.Record.ResponseHeaders {
		headers.Set(key, value)
		private = append(private, value)
	}
	return usage.Record{
		Provider:        fixture.Record.Provider,
		Model:           fixture.Record.Model,
		Alias:           fixture.Record.Alias,
		APIKey:          fixture.Record.APIKey,
		AuthID:          fixture.Record.AuthID,
		AuthType:        fixture.Record.AuthType,
		Source:          fixture.Record.Source,
		RequestedAt:     requestedAt,
		Latency:         time.Duration(fixture.Record.LatencyMS) * time.Millisecond,
		TTFT:            time.Duration(fixture.Record.TTFTMS) * time.Millisecond,
		Failed:          fixture.Record.Failed,
		Fail:            usage.Failure{StatusCode: 500, Body: fixture.Record.FailureBody},
		Detail:          usage.Detail{TokenBreakdown: fixture.Record.TokenBreakdown},
		ResponseHeaders: headers,
	}, private
}

type recordedCall struct {
	command  []string
	document []byte
	env      []string
	deadline time.Time
}

type recordingRunner struct {
	calls []recordedCall
	err   error
}

func (runner *recordingRunner) Run(ctx context.Context, command []string, document []byte, env []string) error {
	deadline, _ := ctx.Deadline()
	runner.calls = append(runner.calls, recordedCall{
		command:  append([]string(nil), command...),
		document: append([]byte(nil), document...),
		env:      append([]string(nil), env...),
		deadline: deadline,
	})
	return runner.err
}

func newFixturePlugin(t *testing.T, runner collectorRunner) (*Plugin, string, string) {
	t.Helper()
	root := t.TempDir()
	collector := filepath.Join(root, "OpenUsage Collector")
	if err := os.WriteFile(collector, []byte("fixture"), 0o700); err != nil {
		t.Fatal(err)
	}
	database := filepath.Join(root, "runtime.sqlite3")
	plugin, err := newWithDependencies(Config{
		CollectorPath: collector,
		DatabasePath:  database,
		ScopeRef:      fixtureScope,
		ProviderMap:   map[string]string{"openai": "openai"},
	}, runner, []byte("openusage-cliproxy-fixture-id-salt"))
	if err != nil {
		t.Fatal(err)
	}
	return plugin, collector, database
}

func TestContentBearingRecordBecomesOneStrictPrivateObservation(t *testing.T) {
	record, privateValues := fixtureRecord(t)
	runner := &recordingRunner{}
	plugin, collector, database := newFixturePlugin(t, runner)

	plugin.HandleUsage(context.Background(), record)

	if len(runner.calls) != 1 {
		t.Fatalf("collector calls = %d, want 1", len(runner.calls))
	}
	call := runner.calls[0]
	wantCommand := []string{collector, "runtime-ingest", "--database", database}
	if strings.Join(call.command, "\x00") != strings.Join(wantCommand, "\x00") {
		t.Fatalf("command = %#v", call.command)
	}
	if time.Until(call.deadline) <= 0 || time.Until(call.deadline) > 3*time.Second {
		t.Fatalf("unexpected deadline: %v", call.deadline)
	}
	if len(call.document) > 1024*1024 {
		t.Fatalf("document too large: %d", len(call.document))
	}
	encoded := string(call.document)
	for _, private := range privateValues {
		if private != "" && strings.Contains(encoded, private) {
			t.Fatalf("private value serialized: %q", private)
		}
	}
	if strings.Contains(encoded, "Authorization") || strings.Contains(encoded, "Set-Cookie") {
		t.Fatal("private header name serialized")
	}
	if got, want := strings.Join(call.env, "\n"), "HOME="+os.Getenv("HOME")+"\nLANG=C.UTF-8\nPATH=/usr/bin:/bin"; got != want {
		t.Fatalf("environment = %q, want %q", got, want)
	}

	var document struct {
		SchemaVersion int `json:"schemaVersion"`
		Observations  []struct {
			ObservationID       string  `json:"observationId"`
			ProviderID          string  `json:"providerId"`
			ModelID             string  `json:"modelId"`
			ScopeRef            string  `json:"scopeRef"`
			StartedAt           string  `json:"startedAt"`
			FirstTokenAt        *string `json:"firstTokenAt"`
			CompletedAt         string  `json:"completedAt"`
			InputTokens         int64   `json:"inputTokens"`
			OutputTokens        int64   `json:"outputTokens"`
			CacheReadTokens     int64   `json:"cacheReadTokens"`
			CacheCreationTokens int64   `json:"cacheCreationTokens"`
			ReasoningTokens     *int64  `json:"reasoningTokens"`
			TotalTokens         int64   `json:"totalTokens"`
			CountingConvention  string  `json:"tokenCountingConvention"`
			Status              string  `json:"status"`
			CostMicros          *int64  `json:"costMicros"`
			CostCurrency        *string `json:"costCurrency"`
			SourceID            string  `json:"sourceId"`
			Quality             string  `json:"quality"`
		} `json:"observations"`
	}
	if err := json.Unmarshal(call.document, &document); err != nil {
		t.Fatal(err)
	}
	if document.SchemaVersion != 1 || len(document.Observations) != 1 {
		t.Fatalf("invalid document header: %#v", document)
	}
	row := document.Observations[0]
	if !strings.HasPrefix(row.ObservationID, "obs_") || len(row.ObservationID) != 36 {
		t.Fatalf("observation id = %q", row.ObservationID)
	}
	if row.ProviderID != "openai" || row.ModelID != "openai.gpt-5" || row.ScopeRef != fixtureScope {
		t.Fatalf("unexpected identity: %#v", row)
	}
	if row.StartedAt != "2026-08-01T00:00:00.000000Z" || row.CompletedAt != "2026-08-01T00:00:03.000000Z" {
		t.Fatalf("unexpected timing: %#v", row)
	}
	if row.FirstTokenAt == nil || *row.FirstTokenAt != "2026-08-01T00:00:00.750000Z" {
		t.Fatalf("unexpected first token: %#v", row.FirstTokenAt)
	}
	if row.InputTokens != 12 || row.OutputTokens != 3 || row.CacheReadTokens != 4 || row.CacheCreationTokens != 0 || row.ReasoningTokens == nil || *row.ReasoningTokens != 1 || row.TotalTokens != 15 {
		t.Fatalf("unexpected counters: %#v", row)
	}
	if row.CountingConvention != "input_includes_cache" || row.Status != "completed" || row.CostMicros != nil || row.CostCurrency != nil || row.SourceID != "cliproxyapi.v7.2.113.usage.v2" || row.Quality != "provider_reported" {
		t.Fatalf("unexpected semantics: %#v", row)
	}
}

func TestMissingIncompleteOrInconsistentAccountingIsDroppedNotZeroFilled(t *testing.T) {
	record, _ := fixtureRecord(t)
	cases := []usage.TokenBreakdown{
		{},
		{SchemaVersion: 2, Quality: usage.TokenAccountingQualityComplete},
		{SchemaVersion: 2, Quality: usage.TokenAccountingQualityUnclassified, TotalTokens: 15, UnclassifiedTokens: 15},
		{SchemaVersion: 2, Quality: usage.TokenAccountingQualityInconsistent, TotalTokens: 15},
		{SchemaVersion: 2, Quality: usage.TokenAccountingQualityComplete, TotalTokens: 16, Input: usage.TokenInputBreakdown{TotalTokens: 12, UncachedTokens: 8, CacheReadTokens: 4}, Output: usage.TokenOutputBreakdown{TotalTokens: 3, NonReasoningTokens: 2, ReasoningTokens: 1}},
	}
	for _, breakdown := range cases {
		runner := &recordingRunner{}
		plugin, _, _ := newFixturePlugin(t, runner)
		candidate := record
		candidate.Detail.TokenBreakdown = breakdown
		plugin.HandleUsage(context.Background(), candidate)
		if len(runner.calls) != 0 {
			t.Fatalf("invalid breakdown delivered: %#v", breakdown)
		}
	}
}

func TestUnknownProviderUnsafeIdentityOrTimingIsDropped(t *testing.T) {
	record, _ := fixtureRecord(t)
	cases := []usage.Record{
		func() usage.Record { value := record; value.Provider = "unknown"; return value }(),
		func() usage.Record { value := record; value.Model = "model with spaces"; return value }(),
		func() usage.Record { value := record; value.RequestedAt = time.Time{}; return value }(),
		func() usage.Record { value := record; value.Latency = -time.Second; return value }(),
		func() usage.Record { value := record; value.TTFT = 4 * time.Second; return value }(),
	}
	for _, candidate := range cases {
		runner := &recordingRunner{}
		plugin, _, _ := newFixturePlugin(t, runner)
		plugin.HandleUsage(context.Background(), candidate)
		if len(runner.calls) != 0 {
			t.Fatalf("unsafe record delivered: %#v", candidate)
		}
	}
}

func TestUnknownTTFTStaysNullAndFailureStatusIsPreserved(t *testing.T) {
	record, _ := fixtureRecord(t)
	record.TTFT = 0
	record.Failed = true
	runner := &recordingRunner{}
	plugin, _, _ := newFixturePlugin(t, runner)

	plugin.HandleUsage(context.Background(), record)

	if len(runner.calls) != 1 {
		t.Fatalf("collector calls = %d", len(runner.calls))
	}
	var document struct {
		Observations []struct {
			FirstTokenAt *string `json:"firstTokenAt"`
			Status       string  `json:"status"`
		} `json:"observations"`
	}
	if err := json.Unmarshal(runner.calls[0].document, &document); err != nil {
		t.Fatal(err)
	}
	if document.Observations[0].FirstTokenAt != nil || document.Observations[0].Status != "error" {
		t.Fatalf("unexpected terminal facts: %#v", document.Observations[0])
	}
}

func TestDeliveryFailureNeverEscapesHandleUsage(t *testing.T) {
	record, _ := fixtureRecord(t)
	runner := &recordingRunner{err: errors.New("private collector failure")}
	plugin, _, _ := newFixturePlugin(t, runner)

	plugin.HandleUsage(context.Background(), record)

	if len(runner.calls) != 1 {
		t.Fatalf("collector calls = %d", len(runner.calls))
	}
}

func TestExecCollectorRunnerUsesOnlyProvidedCommandStdinAndEnvironment(t *testing.T) {
	root := t.TempDir()
	output := filepath.Join(root, "capture")
	script := filepath.Join(root, "collector")
	source := "#!/bin/sh\n/usr/bin/env | /usr/bin/sort > \"$1.env\"\n/bin/cat > \"$1.stdin\"\n"
	if err := os.WriteFile(script, []byte(source), 0o700); err != nil {
		t.Fatal(err)
	}
	document := []byte(`{"schemaVersion":1}`)
	environment := []string{"HOME=" + root, "LANG=C.UTF-8", "PATH=/usr/bin:/bin"}

	err := (execCollectorRunner{}).Run(
		context.Background(), []string{script, output}, document, environment,
	)
	if err != nil {
		t.Fatal(err)
	}
	stdin, err := os.ReadFile(output + ".stdin")
	if err != nil {
		t.Fatal(err)
	}
	env, err := os.ReadFile(output + ".env")
	if err != nil {
		t.Fatal(err)
	}
	if string(stdin) != string(document) {
		t.Fatalf("stdin = %q", stdin)
	}
	workingDirectory, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	wantEnv := "HOME=" + root + "\nLANG=C.UTF-8\nPATH=/usr/bin:/bin\nPWD=" + workingDirectory + "\nSHLVL=1\n_=/usr/bin/env\n"
	if string(env) != wantEnv {
		t.Fatalf("environment = %q, want %q", env, wantEnv)
	}
}

func TestConfigurationRejectsRelativeSymlinkIdentityAndProviderMap(t *testing.T) {
	root := t.TempDir()
	collector := filepath.Join(root, "collector")
	if err := os.WriteFile(collector, []byte("fixture"), 0o700); err != nil {
		t.Fatal(err)
	}
	database := filepath.Join(root, "runtime.sqlite3")
	link := filepath.Join(root, "collector-link")
	if err := os.Symlink(collector, link); err != nil {
		t.Fatal(err)
	}
	cases := []Config{
		{CollectorPath: "collector", DatabasePath: database, ScopeRef: fixtureScope, ProviderMap: map[string]string{"openai": "openai"}},
		{CollectorPath: link, DatabasePath: database, ScopeRef: fixtureScope, ProviderMap: map[string]string{"openai": "openai"}},
		{CollectorPath: collector, DatabasePath: "runtime.sqlite3", ScopeRef: fixtureScope, ProviderMap: map[string]string{"openai": "openai"}},
		{CollectorPath: collector, DatabasePath: database, ScopeRef: "user@example.com", ProviderMap: map[string]string{"openai": "openai"}},
		{CollectorPath: collector, DatabasePath: database, ScopeRef: fixtureScope, ProviderMap: map[string]string{}},
		{CollectorPath: collector, DatabasePath: database, ScopeRef: fixtureScope, ProviderMap: map[string]string{"openai": "openai/account"}},
	}
	for _, config := range cases {
		if _, err := New(config); err == nil {
			t.Fatalf("configuration accepted: %#v", config)
		}
	}
}

var _ usage.Plugin = (*Plugin)(nil)
