use super::{Expr, Program, Result, WorkflowError};

/// Parse interpreter source text into a [`Program`].
pub fn parse_program(source: &str) -> Result<Program> {
    let mut parser = Parser::new(source);
    let mut forms = Vec::new();
    while parser.skip_ws() {
        if parser.eof() {
            break;
        }
        forms.push(parser.parse_expr()?);
    }

    let name = infer_program_name(&forms);
    Ok(Program::new(name, source, forms))
}

fn infer_program_name(forms: &[Expr]) -> String {
    // Prefer an explicit `(workflow <name> …)` declaration.
    for expr in forms {
        if let Expr::List(items) = expr {
            if let Some(Expr::Symbol(head)) = items.first() {
                if (head == "workflow" || head == "program") && items.len() > 1 {
                    if let Expr::Symbol(name) | Expr::String(name) = &items[1] {
                        return name.clone();
                    }
                }
            }
        }
    }

    // Fallback to the first list head symbol if nothing obvious is present.
    if let Some(Expr::List(items)) = forms.first() {
        if let Some(Expr::Symbol(sym)) = items.first() {
            return sym.clone();
        }
    }

    "anonymous".to_string()
}

struct Parser<'a> {
    src: &'a str,
    bytes: &'a [u8],
    index: usize,
}

type ParseResult<T> = std::result::Result<T, WorkflowError>;

impl<'a> Parser<'a> {
    fn new(src: &'a str) -> Self {
        Self {
            src,
            bytes: src.as_bytes(),
            index: 0,
        }
    }

    fn eof(&self) -> bool {
        self.index >= self.bytes.len()
    }

    fn current(&self) -> Option<u8> {
        self.bytes.get(self.index).copied()
    }

    fn advance(&mut self) {
        if self.index < self.bytes.len() {
            self.index += 1;
        }
    }

    fn skip_ws(&mut self) -> bool {
        let mut advanced = false;
        loop {
            while let Some(ch) = self.current() {
                if ch.is_ascii_whitespace() {
                    advanced = true;
                    self.advance();
                } else {
                    break;
                }
            }
            if self.current() == Some(b';') {
                advanced = true;
                while let Some(ch) = self.current() {
                    self.advance();
                    if ch == b'\n' {
                        break;
                    }
                }
                continue;
            }
            break;
        }
        advanced || !self.eof()
    }

    fn parse_expr(&mut self) -> ParseResult<Expr> {
        self.skip_ws();
        if self.eof() {
            return Err(self.error("unexpected end of input"));
        }

        match self.current().unwrap() {
            b'(' => self.parse_list(),
            b'"' => self.parse_string(),
            b':' => self.parse_keyword(),
            b'-' | b'+' | b'0'..=b'9' => self.parse_number_or_symbol(),
            _ => self.parse_symbol_or_bool(),
        }
    }

    fn parse_list(&mut self) -> ParseResult<Expr> {
        // consume '('
        self.advance();
        let mut items = Vec::new();
        loop {
            self.skip_ws();
            if self.eof() {
                return Err(self.error("unterminated list"));
            }
            if self.current() == Some(b')') {
                self.advance();
                break;
            }
            items.push(self.parse_expr()?);
        }
        Ok(Expr::List(items))
    }

    fn parse_string(&mut self) -> ParseResult<Expr> {
        // consume opening quote
        self.advance();
        let mut buf = String::new();
        while let Some(ch) = self.current() {
            self.advance();
            match ch {
                b'"' => return Ok(Expr::String(buf)),
                b'\\' => {
                    let escaped = self
                        .current()
                        .ok_or_else(|| self.error("incomplete escape"))?;
                    self.advance();
                    let value = match escaped {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        other => {
                            return Err(self.error(&format!("unknown escape: \\{}", other as char)));
                        }
                    };
                    buf.push(value);
                }
                _ => buf.push(ch as char),
            }
        }
        Err(self.error("unterminated string literal"))
    }

    fn parse_keyword(&mut self) -> ParseResult<Expr> {
        self.advance(); // consume ':'
        let start = self.index;
        while let Some(ch) = self.current() {
            if is_symbol_char(ch) {
                self.advance();
            } else {
                break;
            }
        }
        if start == self.index {
            return Err(self.error("empty keyword"));
        }
        let text = &self.src[start..self.index];
        Ok(Expr::Keyword(text.to_string()))
    }

    fn parse_number_or_symbol(&mut self) -> ParseResult<Expr> {
        let start = self.index;
        if self.current() == Some(b'-') || self.current() == Some(b'+') {
            self.advance();
        }
        let mut has_digit = false;
        while let Some(ch) = self.current() {
            if ch.is_ascii_digit() {
                has_digit = true;
                self.advance();
            } else {
                break;
            }
        }

        let mut is_float = false;
        if self.current() == Some(b'.') {
            if let Some(next) = self.peek_char() {
                if next.is_ascii_digit() {
                    is_float = true;
                    self.advance();
                    while let Some(ch) = self.current() {
                        if ch.is_ascii_digit() {
                            self.advance();
                        } else {
                            break;
                        }
                    }
                }
            }
        }

        if !has_digit {
            self.index = start;
            return self.parse_symbol_or_bool();
        }

        let text = &self.src[start..self.index];
        if is_float {
            match text.parse::<f64>() {
                Ok(value) => Ok(Expr::Float(value)),
                Err(_) => Err(self.error("invalid float literal")),
            }
        } else {
            match text.parse::<i64>() {
                Ok(value) => Ok(Expr::Integer(value)),
                Err(_) => Err(self.error("invalid integer literal")),
            }
        }
    }

    fn parse_symbol_or_bool(&mut self) -> ParseResult<Expr> {
        let start = self.index;
        while let Some(ch) = self.current() {
            if is_symbol_char(ch) {
                self.advance();
            } else {
                break;
            }
        }
        if start == self.index {
            return Err(self.error("unexpected character"));
        }
        let text = &self.src[start..self.index];
        match text {
            "true" => Ok(Expr::Boolean(true)),
            "false" => Ok(Expr::Boolean(false)),
            _ => Ok(Expr::Symbol(text.to_string())),
        }
    }

    fn peek_char(&self) -> Option<u8> {
        self.bytes.get(self.index + 1).copied()
    }

    fn error(&self, message: &str) -> WorkflowError {
        WorkflowError::Syntax(format!("{} at byte {}", message, self.index))
    }
}

fn is_symbol_char(ch: u8) -> bool {
    match ch {
        b'(' | b')' | b'"' | b';' => false,
        c if c.is_ascii_whitespace() => false,
        _ => true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_simple_program() {
        let src = "(workflow demo (state plan))";
        let program = parse_program(src).expect("parse");
        assert_eq!(program.name, "demo");
        assert_eq!(program.forms.len(), 1);
    }

    #[test]
    fn parses_numbers_strings_and_keywords() {
        let src = "(define value :key 42 \"text\")";
        let program = parse_program(src).expect("parse");
        assert_eq!(program.name, "define");
        assert_eq!(program.forms.len(), 1);
    }

    #[test]
    fn parses_multiple_forms() {
        let src = "(require codebase/transcript)\n(workflow demo)";
        let program = parse_program(src).expect("parse");
        assert_eq!(program.forms.len(), 2);
        assert_eq!(program.name, "demo");
    }
}
