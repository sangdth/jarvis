//! Text helpers shared by the panes.

/// First visible row so the selected row stays on screen (the row itself is the last visible one).
pub fn scroll_offset(selected: usize, len: usize, height: usize) -> usize {
    if len <= height || height == 0 || selected < height {
        0
    } else {
        (selected + 1).saturating_sub(height)
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
}
