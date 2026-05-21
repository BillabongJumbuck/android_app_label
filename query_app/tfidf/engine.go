package tfidf

import (
	"encoding/binary"
	"embed"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"github.com/go-ego/gse"
)

//go:embed data/*
var modelFS embed.FS

var (
	seg     gse.Segmenter
	segOnce sync.Once
)

func initSeg() {
	segOnce.Do(func() {
		s1, _ := modelFS.ReadFile("data/s_1.txt")
		t1, _ := modelFS.ReadFile("data/t_1.txt")

		dir, err := os.MkdirTemp("", "gse_dict")
		if err != nil {
			seg.LoadDict()
			return
		}
		os.WriteFile(filepath.Join(dir, "s_1.txt"), s1, 0644)
		os.WriteFile(filepath.Join(dir, "t_1.txt"), t1, 0644)

		old := log.Writer()
		log.SetOutput(io.Discard)
		seg.LoadDict(filepath.Join(dir, "s_1.txt"), filepath.Join(dir, "t_1.txt"))
		log.SetOutput(old)
	})
}

// ── Full k-NN model ────────────────────────────────────────

type Model struct {
	Vocab   map[string]int
	IDF     []float64
	Labels  []string
	Records []SparseVec
	Norms   []float64
}

type SparseVec struct {
	LabelIdx uint16
	FeatIdx  []uint16
	Weights  []float32
}

func LoadModel() (*Model, error) {
	initSeg()
	m := &Model{}

	b, _ := modelFS.ReadFile("data/vocab.json")
	json.Unmarshal(b, &m.Vocab)
	b, _ = modelFS.ReadFile("data/idf.json")
	json.Unmarshal(b, &m.IDF)
	b, _ = modelFS.ReadFile("data/labels.json")
	json.Unmarshal(b, &m.Labels)

	raw, err := modelFS.ReadFile("data/records.bin")
	if err != nil {
		return nil, fmt.Errorf("records: %w", err)
	}
	return m, m.loadRecords(raw)
}

func (m *Model) loadRecords(data []byte) error {
	if len(data) < 4 {
		return fmt.Errorf("too short")
	}
	n := int(binary.LittleEndian.Uint32(data[:4]))
	pos := 4
	m.Norms = make([]float64, n)

	for i := 0; i < n; i++ {
		if pos+4 > len(data) {
			return fmt.Errorf("truncated at %d", i)
		}
		labelIdx := binary.LittleEndian.Uint16(data[pos : pos+2])
		nnz := binary.LittleEndian.Uint16(data[pos+2 : pos+4])
		pos += 4

		vec := SparseVec{LabelIdx: labelIdx}
		var norm float64
		for j := 0; j < int(nnz); j++ {
			feat := binary.LittleEndian.Uint16(data[pos : pos+2])
			weight := math.Float32frombits(binary.LittleEndian.Uint32(data[pos+2 : pos+6]))
			pos += 6
			vec.FeatIdx = append(vec.FeatIdx, feat)
			vec.Weights = append(vec.Weights, weight)
			norm += float64(weight) * float64(weight)
		}
		m.Records = append(m.Records, vec)
		m.Norms[i] = math.Sqrt(norm)
	}
	return nil
}

// ── Tokenization ───────────────────────────────────────────

func tokenize(text string) []string {
	words := seg.Cut(text, true)
	var tokens []string
	for _, w := range words {
		w = strings.TrimSpace(w)
		if len([]rune(w)) >= 2 {
			tokens = append(tokens, w)
		}
	}
	clean := strings.ReplaceAll(text, " ", "")
	runes := []rune(clean)
	for i := 0; i < len(runes)-1; i++ {
		tokens = append(tokens, string(runes[i:i+2]))
	}
	return tokens
}

func permKeywords(perms string) []string {
	if perms == "" || perms == "<NONE>" || perms == "<BLOCKED>" {
		return nil
	}
	var kws []string
	for _, p := range strings.Split(perms, "|") {
		p = strings.TrimSpace(strings.ToLower(p))
		p = strings.ReplaceAll(p, "android.permission.", "")
		var sb strings.Builder
		for _, ch := range p {
			if ch >= 'A' && ch <= 'Z' {
				sb.WriteByte(' ')
			}
			sb.WriteRune(ch)
		}
		p = strings.ReplaceAll(sb.String(), "_", " ")
		p = strings.Join(strings.Fields(p), " ")
		if len(p) > 2 {
			kws = append(kws, p)
		}
	}
	return kws
}

func buildFeatures(name, perms string) []string {
	parts := []string{name, name}
	pk := permKeywords(perms)
	if len(pk) > 0 {
		parts = append(parts, strings.Join(pk, " "))
	}
	return tokenize(strings.Join(parts, " "))
}

// ── Inference (full k-NN) ──────────────────────────────────

type Prediction struct {
	Label string
	Score float64
}

func (m *Model) Predict(appName, permissions string, topK int) []Prediction {
	tokens := buildFeatures(appName, permissions)

	qVec := make(map[uint16]float64)
	for _, tok := range tokens {
		idx, ok := m.Vocab[tok]
		if !ok {
			continue
		}
		qVec[uint16(idx)] += 1.0
	}
	var qNorm float64
	for idx, tf := range qVec {
		w := (1 + math.Log(tf)) * m.IDF[idx]
		qVec[idx] = w
		qNorm += w * w
	}
	qNorm = math.Sqrt(qNorm)
	if qNorm < 1e-10 {
		return nil
	}

	type scored struct {
		pred  Prediction
		score float64
	}
	var best []scored

	for i, rec := range m.Records {
		if m.Norms[i] < 1e-10 {
			continue
		}
		var dot float64
		for j, fidx := range rec.FeatIdx {
			if qw, ok := qVec[fidx]; ok {
				dot += qw * float64(rec.Weights[j])
			}
		}
		sim := dot / (qNorm * m.Norms[i])
		if sim < 0.03 {
			continue
		}

		cand := scored{pred: Prediction{Label: m.Labels[rec.LabelIdx], Score: sim}, score: sim}
		if len(best) < topK {
			best = append(best, cand)
		} else {
			lo := 0
			for j := 1; j < len(best); j++ {
				if best[j].score < best[lo].score {
					lo = j
				}
			}
			if sim > best[lo].score {
				best[lo] = cand
			}
		}
	}

	sort.Slice(best, func(i, j int) bool { return best[i].score > best[j].score })
	out := make([]Prediction, len(best))
	for i, b := range best {
		out[i] = b.pred
	}
	return out
}

func (m *Model) BestLabel(appName, permissions string) string {
	preds := m.Predict(appName, permissions, 8)
	if len(preds) == 0 {
		return "unknown"
	}
	votes := make(map[string]float64)
	for _, p := range preds {
		votes[p.Label] += p.Score
	}
	bestL := ""
	bestS := 0.0
	for l, s := range votes {
		if s > bestS {
			bestS = s
			bestL = l
		}
	}
	if bestS < 0.08 {
		return "unknown"
	}
	return bestL
}
