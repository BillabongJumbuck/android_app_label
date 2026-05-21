package main

import (
	"database/sql"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"sync"

	_ "modernc.org/sqlite"
	"query_app/tfidf"
)

const dbFile = "apps.db"

var (
	tfidfModel     *tfidf.Model
	tfidfModelOnce sync.Once
)

func getModel() *tfidf.Model {
	tfidfModelOnce.Do(func() {
		var err error
		tfidfModel, err = tfidf.LoadModel()
		if err != nil {
			fmt.Fprintf(os.Stderr, "TF-IDF model load failed: %v (predictions disabled)\n", err)
		}
	})
	return tfidfModel
}

var csvFiles = map[string]string{
	"xiaomi":      "./mi_apps_full.csv",
	"google_play": "./android_apps_with_perms.csv",
	"taptap":      "./taptap_apps.csv",
	"yyb":         "./yyb_apps.csv",
}

func main() {
	labelOnly := flag.Bool("l", false, "output only labels (comma-separated if multiple)")
	flag.Parse()

	if flag.NArg() < 1 {
		fmt.Fprintln(os.Stderr, "Usage: query_app [-l] <package_name> [package_name2 ...]")
		os.Exit(1)
	}

	db, err := sql.Open("sqlite", dbFile)
	if err != nil {
		fatalf("open db: %v", err)
	}
	defer db.Close()

	if err := initDB(db); err != nil {
		fatalf("init db: %v", err)
	}

	for _, pkg := range flag.Args() {
		query(db, pkg, *labelOnly)
	}
}

func initDB(db *sql.DB) error {
	var count int
	if err := db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='apps'").Scan(&count); err != nil {
		return fmt.Errorf("check table: %w", err)
	}
	if count > 0 {
		return nil // already initialized
	}

	fmt.Fprintln(os.Stderr, "creating database and importing CSVs...")

	if _, err := db.Exec(`
		CREATE TABLE apps (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			package_name TEXT NOT NULL,
			app_name TEXT,
			label TEXT,
			developer TEXT,
			permissions TEXT,
			source TEXT NOT NULL
		);
		CREATE INDEX idx_package_name ON apps(package_name);
	`); err != nil {
		return fmt.Errorf("create table: %w", err)
	}

	for source, path := range csvFiles {
		n, err := importCSV(db, path, source)
		if err != nil {
			return fmt.Errorf("import %s: %w", source, err)
		}
		fmt.Fprintf(os.Stderr, "  imported %d records from %s\n", n, source)
	}
	return nil
}

func importCSV(db *sql.DB, path, source string) (int, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()

	r := csv.NewReader(f)
	headers, err := r.Read()
	if err != nil {
		return 0, err
	}

	colIdx := map[string]int{}
	for i, h := range headers {
		colIdx[strings.TrimSpace(h)] = i
	}

	tx, err := db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	stmt, err := tx.Prepare(`INSERT INTO apps (package_name, app_name, label, developer, permissions, source)
		VALUES (?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return 0, err
	}
	defer stmt.Close()

	count := 0
	for {
		row, err := r.Read()
		if err != nil {
			break
		}
		pkg := row[colIdx["package_name"]]
		if pkg == "" {
			continue
		}
		label := row[colIdx["label"]]
		if source == "taptap" && label != "" {
			label = "游戏/" + label
		}
		perm := row[colIdx["permissions"]]
		// 过滤无效标记
		if perm == "<NONE>" || perm == "<BLOCKED>" {
			perm = ""
		}

		if _, err := stmt.Exec(
			pkg,
			row[colIdx["app_name"]],
			label,
			row[colIdx["developer"]],
			perm,
			source,
		); err != nil {
			return 0, fmt.Errorf("insert %s: %w", pkg, err)
		}
		count++
	}

	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return count, nil
}

type result struct {
	PackageName string   `json:"package_name"`
	AppName     string   `json:"app_name,omitempty"`
	Label       string   `json:"label,omitempty"`
	Developer   string   `json:"developer,omitempty"`
	Permissions []string `json:"permissions,omitempty"`
	Source      string   `json:"source,omitempty"`
	NotFound    bool     `json:"not_found,omitempty"`
}

func query(db *sql.DB, pkg string, labelOnly bool) {
	rows, err := db.Query(`SELECT app_name, label, developer, permissions, source
		FROM apps WHERE package_name = ? ORDER BY source`, pkg)
	if err != nil {
		fmt.Fprintf(os.Stderr, "query error: %v\n", err)
		return
	}
	defer rows.Close()

	var results []result
	for rows.Next() {
		var name, label, dev, perms, source string
		if err := rows.Scan(&name, &label, &dev, &perms, &source); err != nil {
			fmt.Fprintf(os.Stderr, "scan error: %v\n", err)
			return
		}
		r := result{
			PackageName: pkg,
			AppName:     name,
			Label:       label,
			Developer:   dev,
			Source:      source,
		}
		if perms != "" {
			for _, p := range strings.Split(perms, " | ") {
				r.Permissions = append(r.Permissions, strings.TrimSpace(p))
			}
		}
		results = append(results, r)
	}

	if labelOnly {
		if len(results) == 0 {
fmt.Println(predictLabel(pkg, ""))
			return
		}
		var labels []string
		for _, r := range results {
			if r.Label != "" {
				labels = append(labels, r.Label)
			}
		}
		fmt.Println(strings.Join(labels, ","))
		return
	}

	if len(results) == 0 {
		predicted := predictLabel(pkg, "")
		fmt.Println(predicted)
		return
	}

	out, _ := json.MarshalIndent(results, "", "  ")
	fmt.Println(string(out))
}

var genericSegments = map[string]bool{
	"android": true, "mobile": true, "app": true, "ui": true,
	"community": true, "service": true, "services": true,
	"application": true, "client": true, "platform": true,
	"com": true, "cn": true, "org": true, "net": true, "io": true,
}

func inferName(pkg string) string {
	parts := strings.Split(pkg, ".")
	// Walk backwards, skip generic segments
	for i := len(parts) - 1; i >= 0; i-- {
		if !genericSegments[parts[i]] {
			return parts[i]
		}
	}
	return parts[len(parts)-1]
}

func predictLabel(pkg, appName string) string {
	// Infer name from package if not provided
	if appName == "" {
		appName = inferName(pkg)
	}

	// 1. OEM rules
	if label, ok := tfidf.MatchOEM(appName, pkg); ok {
		return "[OEM] " + label
	}

	// 2. TF-IDF model
	m := getModel()
	if m == nil {
		return "(no result)"
	}
	label := m.BestLabel(appName, "")
	if label == "unknown" || label == "" {
		return "(no result)"
	}
	return "[TF-IDF] " + label
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
