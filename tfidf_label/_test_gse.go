package main

import (
	"fmt"
	"strings"

	"github.com/go-ego/gse"
)

func main() {
	var seg gse.Segmenter
	seg.LoadDict()

	tests := []string{
		"文件管理",
		"安全中心",
		"垃圾清理",
		"搜狗输入法",
		"小爱语音助手",
		"屏幕录制",
		"游戏中心",
		"浏览器视频",
		"超星学习通",
		"指南针",
		"天气",
		"时钟",
	}

	for _, s := range tests {
		words := seg.Cut(s, true)
		// Filter words >= 2 chars + char bigrams
		var tokens []string
		for _, w := range words {
			w = strings.TrimSpace(w)
			if len([]rune(w)) >= 2 {
				tokens = append(tokens, w)
			}
		}
		// Add char bigrams as fallback
		clean := strings.ReplaceAll(s, " ", "")
		runes := []rune(clean)
		for i := 0; i < len(runes)-1; i++ {
			tokens = append(tokens, string(runes[i:i+2]))
		}
		fmt.Printf("%-20s -> %s\n", s, strings.Join(tokens, " | "))
	}
}
