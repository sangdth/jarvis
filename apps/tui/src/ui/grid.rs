//! A tiny character grid so hand-drawn schematics can be rendered through a `Paragraph`
//! without touching `Buffer` internals. Adjacent cells with identical styles merge into spans.

use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};

pub struct Canvas {
    width: usize,
    height: usize,
    cells: Vec<(char, Style)>,
}

impl Canvas {
    pub fn new(width: usize, height: usize) -> Self {
        Self {
            width,
            height,
            cells: vec![(' ', Style::default()); width * height],
        }
    }

    fn index(&self, x: usize, y: usize) -> Option<usize> {
        if x < self.width && y < self.height {
            Some(y * self.width + x)
        } else {
            None
        }
    }

    pub fn put(&mut self, x: usize, y: usize, ch: char, style: Style) {
        if let Some(idx) = self.index(x, y) {
            self.cells[idx] = (ch, style);
        }
    }

    /// Writes a single-line string starting at `(x, y)`, clipped to the canvas.
    pub fn text(&mut self, x: usize, y: usize, text: &str, style: Style) {
        for (offset, ch) in text.chars().enumerate() {
            if ch == '\n' {
                break;
            }
            self.put(x + offset, y, ch, style);
        }
    }

    pub fn hline(&mut self, x0: usize, x1: usize, y: usize, ch: char, style: Style) {
        if x1 < x0 {
            return;
        }
        for x in x0..=x1 {
            self.put(x, y, ch, style);
        }
    }

    pub fn vline(&mut self, x: usize, y0: usize, y1: usize, ch: char, style: Style) {
        if y1 < y0 {
            return;
        }
        for y in y0..=y1 {
            self.put(x, y, ch, style);
        }
    }

    pub fn rect(&mut self, x: usize, y: usize, w: usize, h: usize, style: Style, double: bool) {
        if w < 2 || h < 2 {
            return;
        }
        let (tl, tr, bl, br, hz, vt) = if double {
            ('╔', '╗', '╚', '╝', '═', '║')
        } else {
            ('┌', '┐', '└', '┘', '─', '│')
        };
        self.put(x, y, tl, style);
        self.put(x + w - 1, y, tr, style);
        self.put(x, y + h - 1, bl, style);
        self.put(x + w - 1, y + h - 1, br, style);
        self.hline(x + 1, x + w - 2, y, hz, style);
        self.hline(x + 1, x + w - 2, y + h - 1, hz, style);
        self.vline(x, y + 1, y + h - 2, vt, style);
        self.vline(x + w - 1, y + 1, y + h - 2, vt, style);
    }

    pub fn into_text(self) -> Text<'static> {
        let mut lines = Vec::with_capacity(self.height);
        for y in 0..self.height {
            let mut spans: Vec<Span<'static>> = Vec::new();
            let mut run = String::new();
            let mut run_style = self.cells[y * self.width].1;
            for x in 0..self.width {
                let (ch, style) = self.cells[y * self.width + x];
                if style == run_style {
                    run.push(ch);
                } else {
                    if !run.is_empty() {
                        spans.push(Span::styled(std::mem::take(&mut run), run_style));
                    }
                    run.push(ch);
                    run_style = style;
                }
            }
            if !run.is_empty() {
                spans.push(Span::styled(run, run_style));
            }
            lines.push(Line::from(spans));
        }
        Text::from(lines)
    }
}

/// Word-wraps `text` into lines no wider than `width` columns.
pub fn wrap_text(text: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return Vec::new();
    }
    let mut lines = Vec::new();
    for paragraph in text.split('\n') {
        if paragraph.is_empty() {
            lines.push(String::new());
            continue;
        }
        let mut current = String::new();
        let mut current_len = 0usize;
        for word in paragraph.split(' ') {
            let word_len = word.chars().count();
            if word_len > width {
                if !current.is_empty() {
                    lines.push(std::mem::take(&mut current));
                    current_len = 0;
                }
                let mut chunk = String::new();
                for ch in word.chars() {
                    chunk.push(ch);
                    if chunk.chars().count() >= width {
                        lines.push(std::mem::take(&mut chunk));
                    }
                }
                if !chunk.is_empty() {
                    current = chunk;
                    current_len = current.chars().count();
                }
                continue;
            }
            let needed = if current_len == 0 { word_len } else { word_len + 1 };
            if current_len + needed > width {
                lines.push(std::mem::take(&mut current));
                current_len = 0;
            }
            if current_len > 0 {
                current.push(' ');
                current_len += 1;
            }
            current.push_str(word);
            current_len += word_len;
        }
        lines.push(current);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wraps_long_words_by_hard_split() {
        let lines = wrap_text("abcdefghij klm", 4);
        assert_eq!(lines, vec!["abcd", "efgh", "ij", "klm"]);
    }

    #[test]
    fn wraps_on_word_boundaries() {
        let lines = wrap_text("hello brave new world", 11);
        assert_eq!(lines, vec!["hello brave", "new world"]);
    }

    #[test]
    fn preserves_blank_lines() {
        let lines = wrap_text("a\n\nb", 10);
        assert_eq!(lines, vec!["a", "", "b"]);
    }

    #[test]
    fn canvas_renders_text_and_borders() {
        let mut canvas = Canvas::new(6, 3);
        canvas.rect(0, 0, 6, 3, Style::default(), false);
        canvas.text(1, 1, "hi", Style::default());
        let text = canvas.into_text();
        assert_eq!(text.lines.len(), 3);
    }
}
