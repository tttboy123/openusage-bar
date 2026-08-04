// Package openusageplugin provides a privacy-bounded CLIProxyAPI usage.Plugin.
//
// The plugin deliberately inspects only public provider/model values, terminal
// timing, status and canonical TokenBreakdown v2. It never serializes the full
// usage.Record, which can contain credentials, headers and failure bodies.
package openusageplugin

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/usage"
)

const (
	schemaVersion     = 1
	sourceID          = "cliproxyapi.v7.2.113.usage.v2"
	maxCounter        = int64(1_000_000_000_000)
	maxDocumentBytes  = 1024 * 1024
	collectorDeadline = 3 * time.Second
)

var (
	stableID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	scopeRef = regexp.MustCompile(`^anon_[0-9a-f]{16,64}$`)
)

// Config fixes the local Collector target and the anonymous Provider mapping.
// ProviderMap keys are CLIProxyAPI Provider values; values are OpenUsage IDs.
type Config struct {
	CollectorPath string
	DatabasePath  string
	ScopeRef      string
	ProviderMap   map[string]string
}

type collectorRunner interface {
	Run(ctx context.Context, command []string, document []byte, env []string) error
}

type execCollectorRunner struct{}

func (execCollectorRunner) Run(ctx context.Context, command []string, document []byte, env []string) error {
	if len(command) == 0 {
		return errors.New("collector command unavailable")
	}
	cmd := exec.CommandContext(ctx, command[0], command[1:]...)
	cmd.Stdin = bytes.NewReader(document)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	cmd.Env = append([]string(nil), env...)
	return cmd.Run()
}

// Plugin is a best-effort CLIProxyAPI usage sink. HandleUsage never returns an
// error to the proxy request path.
type Plugin struct {
	collector   string
	database    string
	scope       string
	providers   map[string]string
	environment []string
	runner      collectorRunner
	idSalt      []byte
	sequence    atomic.Uint64
}

type runtimeDocument struct {
	SchemaVersion int                  `json:"schemaVersion"`
	Observations  []runtimeObservation `json:"observations"`
}

type runtimeObservation struct {
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
}

// New validates a fail-closed configuration and returns an official
// CLIProxyAPI usage.Plugin implementation.
func New(config Config) (*Plugin, error) {
	salt := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, salt); err != nil {
		return nil, errors.New("OpenUsage plugin initialization failed")
	}
	return newWithDependencies(config, execCollectorRunner{}, salt)
}

func newWithDependencies(config Config, runner collectorRunner, salt []byte) (*Plugin, error) {
	if !validConfig(config) || runner == nil || len(salt) < 16 || len(salt) > 64 {
		return nil, errors.New("invalid OpenUsage CLIProxyAPI plugin configuration")
	}
	home, err := os.UserHomeDir()
	if err != nil || !filepath.IsAbs(home) {
		return nil, errors.New("invalid OpenUsage CLIProxyAPI plugin configuration")
	}
	providers := make(map[string]string, len(config.ProviderMap))
	for source, target := range config.ProviderMap {
		providers[source] = target
	}
	return &Plugin{
		collector: config.CollectorPath,
		database:  config.DatabasePath,
		scope:     config.ScopeRef,
		providers: providers,
		environment: []string{
			"HOME=" + home,
			"LANG=C.UTF-8",
			"PATH=/usr/bin:/bin",
		},
		runner: runner,
		idSalt: append([]byte(nil), salt...),
	}, nil
}

func validConfig(config Config) bool {
	if !validExecutable(config.CollectorPath) || !validDatabase(config.DatabasePath) ||
		!scopeRef.MatchString(config.ScopeRef) || len(config.ProviderMap) == 0 {
		return false
	}
	for source, target := range config.ProviderMap {
		if !stableID.MatchString(source) || !stableID.MatchString(target) {
			return false
		}
	}
	return true
}

func validExecutable(path string) bool {
	if !filepath.IsAbs(path) {
		return false
	}
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode()&os.ModeSymlink == 0 && info.Mode().Perm()&0o111 != 0
}

func validDatabase(path string) bool {
	if !filepath.IsAbs(path) {
		return false
	}
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
			return false
		}
	} else if !os.IsNotExist(err) {
		return false
	}
	parent, err := os.Stat(filepath.Dir(path))
	return err == nil && parent.IsDir()
}

// HandleUsage implements usage.Plugin. Any invalid record or delivery failure
// is contained locally and cannot alter the proxy response.
func (plugin *Plugin) HandleUsage(_ context.Context, record usage.Record) {
	if plugin == nil {
		return
	}
	defer func() {
		_ = recover()
	}()
	row, ok := plugin.observation(record)
	if !ok {
		return
	}
	document, err := json.Marshal(runtimeDocument{
		SchemaVersion: schemaVersion,
		Observations:  []runtimeObservation{row},
	})
	if err != nil || len(document) > maxDocumentBytes {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), collectorDeadline)
	defer cancel()
	_ = plugin.runner.Run(
		ctx,
		[]string{plugin.collector, "runtime-ingest", "--database", plugin.database},
		document,
		plugin.environment,
	)
}

func (plugin *Plugin) observation(record usage.Record) (runtimeObservation, bool) {
	providerID, ok := plugin.providers[record.Provider]
	if !ok || !stableID.MatchString(providerID) {
		return runtimeObservation{}, false
	}
	modelPart := strings.ReplaceAll(record.Model, "/", ".")
	if !stableID.MatchString(modelPart) {
		return runtimeObservation{}, false
	}
	modelID := modelPart
	if modelPart != providerID && !strings.HasPrefix(modelPart, providerID+".") {
		modelID = providerID + "." + modelPart
	}
	if !stableID.MatchString(modelID) || record.RequestedAt.IsZero() || record.Latency < 0 ||
		record.Latency > 24*time.Hour || record.TTFT < 0 || record.TTFT > record.Latency {
		return runtimeObservation{}, false
	}
	breakdown := record.Detail.TokenBreakdown
	if !breakdown.Valid() || breakdown.SchemaVersion != usage.TokenAccountingSchemaVersion ||
		breakdown.Quality != usage.TokenAccountingQualityComplete || breakdown.UnclassifiedTokens != 0 ||
		breakdown.TotalTokens <= 0 || !boundedBreakdown(breakdown) {
		return runtimeObservation{}, false
	}
	started := record.RequestedAt.UTC()
	completed := started.Add(record.Latency)
	var firstTokenAt *string
	if record.TTFT > 0 {
		value := canonicalTime(started.Add(record.TTFT))
		firstTokenAt = &value
	}
	reasoning := breakdown.Output.ReasoningTokens
	status := "completed"
	if record.Failed {
		status = "error"
	}
	return runtimeObservation{
		ObservationID:       plugin.observationID(),
		ProviderID:          providerID,
		ModelID:             modelID,
		ScopeRef:            plugin.scope,
		StartedAt:           canonicalTime(started),
		FirstTokenAt:        firstTokenAt,
		CompletedAt:         canonicalTime(completed),
		InputTokens:         breakdown.Input.TotalTokens,
		OutputTokens:        breakdown.Output.TotalTokens,
		CacheReadTokens:     breakdown.Input.CacheReadTokens,
		CacheCreationTokens: breakdown.Input.CacheWriteTokens,
		ReasoningTokens:     &reasoning,
		TotalTokens:         breakdown.TotalTokens,
		CountingConvention:  "input_includes_cache",
		Status:              status,
		CostMicros:          nil,
		CostCurrency:        nil,
		SourceID:            sourceID,
		Quality:             "provider_reported",
	}, true
}

func boundedBreakdown(value usage.TokenBreakdown) bool {
	values := []int64{
		value.TotalTokens,
		value.UnclassifiedTokens,
		value.Input.TotalTokens,
		value.Input.UncachedTokens,
		value.Input.CacheReadTokens,
		value.Input.CacheWriteTokens,
		value.Output.TotalTokens,
		value.Output.NonReasoningTokens,
		value.Output.ReasoningTokens,
	}
	for _, counter := range values {
		if counter < 0 || counter > maxCounter {
			return false
		}
	}
	return true
}

func canonicalTime(value time.Time) string {
	return value.UTC().Format("2006-01-02T15:04:05.000000Z")
}

func (plugin *Plugin) observationID() string {
	sequence := plugin.sequence.Add(1)
	var material [8]byte
	binary.BigEndian.PutUint64(material[:], sequence)
	digest := hmac.New(sha256.New, plugin.idSalt)
	_, _ = digest.Write(material[:])
	return "obs_" + hex.EncodeToString(digest.Sum(nil))[:32]
}

var _ usage.Plugin = (*Plugin)(nil)
