use std::collections::BTreeMap;

use super::{ast::Expr, Program, Result, WorkflowError};
use crate::interpreter::ir::{Action, BranchArm, Condition, Instruction, ProgramIr, RoleBinding, State, WaitCondition};

/// Build a typed IR from a parsed program.
pub fn build_ir(program: &Program) -> Result<ProgramIr> {
    let mut metadata = BTreeMap::new();
    let mut roles = Vec::new();
    let mut states = Vec::new();

    for form in &program.forms {
        match form {
            Expr::List(items) if matches_symbol(items.first(), "metadata") => {
                for entry in &items[1..] {
                    if let Expr::List(pair) = entry {
                        if pair.len() == 2 {
                            let key = expect_string(&pair[0])?;
                            let value = expect_string(&pair[1])?;
                            metadata.insert(key, value);
                        } else {
                            return Err(validation("metadata entries must be (key value)"));
                        }
                    } else {
                        return Err(validation("metadata expects (key value) pairs"));
                    }
                }
            }
            Expr::List(items) if matches_symbol(items.first(), "roles") => {
                for role_form in &items[1..] {
                    roles.push(parse_role(role_form)?);
                }
            }
            Expr::List(items) if matches_symbol(items.first(), "state") => {
                states.push(parse_state(items)?);
            }
            Expr::List(items) if matches_symbol(items.first(), "workflow") => {
                // already handled program name
                continue;
            }
            _ => {}
        }
    }

    if states.is_empty() {
        return Err(validation("program must declare at least one state"));
    }

    Ok(ProgramIr {
        name: program.name.clone(),
        metadata,
        roles,
        states,
    })
}

fn parse_role(expr: &Expr) -> Result<RoleBinding> {
    let list = expect_list(expr, "role")?;
    if list.len() < 2 {
        return Err(validation("role requires a name"));
    }
    let name = expect_symbol(&list[0])?;
    let mut props = BTreeMap::new();
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        if idx >= list.len() {
            return Err(validation("role property missing value"));
        }
        let value = expect_string(&list[idx])?;
        idx += 1;
        props.insert(key, value);
    }
    Ok(RoleBinding { name, properties: props })
}

fn parse_state(items: &[Expr]) -> Result<State> {
    if items.len() < 2 {
        return Err(validation("state requires a name"));
    }
    let name = expect_symbol(&items[1])?;
    let mut entry = Vec::new();
    let mut body = Vec::new();
    let mut terminal = false;

    for form in &items[2..] {
        if let Expr::List(list) = form {
            if matches_symbol(list.first(), "enter") {
                for action in &list[1..] {
                    entry.push(parse_action(action)?);
                }
                continue;
            }
            if matches_symbol(list.first(), "terminal") {
                terminal = true;
                continue;
            }
        }
        append_instruction(&mut body, form)?;
    }

    Ok(State { name, entry, body, terminal })
}

fn parse_instruction(expr: &Expr) -> Result<Instruction> {
    if let Expr::List(list) = expr {
        if matches_symbol(list.first(), "await") {
            if list.len() != 2 {
                return Err(validation("await expects a single condition"));
            }
            return Ok(Instruction::Await(parse_wait(&list[1])?));
        }
        if matches_symbol(list.first(), "branch") {
            return parse_branch(&list[1..]);
        }
        if matches_symbol(list.first(), "loop") {
            let mut loop_body = Vec::new();
            for item in &list[1..] {
                append_instruction(&mut loop_body, item)?;
            }
            return Ok(Instruction::Loop(loop_body));
        }
        if matches_symbol(list.first(), "goto") {
            if list.len() != 2 {
                return Err(validation("goto expects a state name"));
            }
            return Ok(Instruction::Transition(expect_symbol(&list[1])?));
        }
    }
    Ok(Instruction::Action(parse_action(expr)?))
}

fn parse_branch(arms: &[Expr]) -> Result<Instruction> {
    let mut branches = Vec::new();
    let mut otherwise = None;
    for arm in arms {
        if let Expr::List(list) = arm {
            if matches_symbol(list.first(), "when") {
                if list.len() < 3 {
                    return Err(validation("when requires condition and body"));
                }
                let cond = parse_condition(&list[1])?;
                let mut body = Vec::new();
                for instr in &list[2..] {
                    append_instruction(&mut body, instr)?;
                }
                branches.push(BranchArm { condition: cond, body });
            } else if matches_symbol(list.first(), "otherwise") {
                let mut body = Vec::new();
                for instr in &list[1..] {
                    append_instruction(&mut body, instr)?;
                }
                otherwise = Some(body);
            } else {
                return Err(validation("unknown branch arm"));
            }
        } else {
            return Err(validation("branch arms must be lists"));
        }
    }
    Ok(Instruction::Branch { arms: branches, otherwise })
}

fn parse_action(expr: &Expr) -> Result<Action> {
    let list = expect_list(expr, "action")?;
    if list.is_empty() {
        return Err(validation("action list cannot be empty"));
    }
    let head = expect_symbol(&list[0])?;
    match head.as_str() {
        "send-prompt" => {
            let mut agent_role = None;
            let mut template = None;
            let mut tag = None;
            let mut idx = 1;
            while idx < list.len() {
                let key = expect_keyword(&list[idx])?;
                idx += 1;
                match key.as_str() {
                    "agent" => {
                        agent_role = Some(expect_symbol(&list[idx])?);
                        idx += 1;
                    }
                    "template" => {
                        template = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    "tag" => {
                        tag = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    _ => return Err(validation("unknown send-prompt argument")),
                }
            }
            Ok(Action::SendPrompt {
                agent_role: agent_role.ok_or_else(|| validation("send-prompt requires :agent"))?,
                template: template.ok_or_else(|| validation("send-prompt requires :template"))?,
                tag,
            })
        }
        "invoke-tool" => {
            let mut role = None;
            let mut capability = None;
            let mut tag = None;
            let mut idx = 1;
            while idx < list.len() {
                let key = expect_keyword(&list[idx])?;
                idx += 1;
                match key.as_str() {
                    "role" => {
                        role = Some(expect_symbol(&list[idx])?);
                        idx += 1;
                    }
                    "capability" => {
                        capability = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    "tag" => {
                        tag = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    _ => return Err(validation("unknown invoke-tool argument")),
                }
            }
            Ok(Action::InvokeTool {
                role: role.ok_or_else(|| validation("invoke-tool requires :role"))?,
                capability: capability.ok_or_else(|| validation("invoke-tool requires :capability"))?,
                tag,
            })
        }
        "emit" => {
            if list.len() != 2 {
                return Err(validation("emit expects a single expression"));
            }
            match &list[1] {
                Expr::List(inner) if matches_symbol(inner.first(), "log") => {
                    if inner.len() != 2 {
                        return Err(validation("log expects a string message"));
                    }
                    Ok(Action::EmitLog(expect_string(&inner[1])?))
                }
                Expr::List(inner) if matches_symbol(inner.first(), "assert") => {
                    if inner.len() != 2 {
                        return Err(validation("assert expects a value"));
                    }
                    Ok(Action::Assert(render_expr(&inner[1])))
                }
                Expr::List(inner) if matches_symbol(inner.first(), "retract") => {
                    if inner.len() != 2 {
                        return Err(validation("retract expects a value"));
                    }
                    Ok(Action::Retract(render_expr(&inner[1])))
                }
                _ => Err(validation("unknown emit form")),
            }
        }
        _ => Err(validation("unknown action")),
    }
}

fn parse_wait(expr: &Expr) -> Result<WaitCondition> {
    let list = expect_list(expr, "wait" )?;
    if list.is_empty() {
        return Err(validation("wait condition cannot be empty"));
    }
    let head = expect_symbol(&list[0])?;
    match head.as_str() {
        "transcript-response" => {
            let mut tag = None;
            let mut idx = 1;
            while idx < list.len() {
                let key = expect_keyword(&list[idx])?;
                idx += 1;
                match key.as_str() {
                    "tag" => {
                        tag = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    _ => return Err(validation("unknown transcript-response argument")),
                }
            }
            Ok(WaitCondition::TranscriptResponse {
                tag: tag.ok_or_else(|| validation("transcript-response requires :tag"))?,
            })
        }
        "signal" => {
            if list.len() < 2 {
                return Err(validation("signal requires a label"));
            }
            Ok(WaitCondition::Signal {
                label: expect_symbol(&list[1])?,
            })
        }
        _ => Err(validation("unknown wait condition")),
    }
}

fn parse_condition(expr: &Expr) -> Result<Condition> {
    let list = expect_list(expr, "condition")?;
    if list.is_empty() {
        return Err(validation("condition cannot be empty"));
    }
    let head = expect_symbol(&list[0])?;
    match head.as_str() {
        "signal" => {
            if list.len() < 2 {
                return Err(validation("signal condition requires label"));
            }
            Ok(Condition::Signal {
                label: expect_symbol(&list[1])?,
            })
        }
        _ => Err(validation("unknown condition")),
    }
}

fn matches_symbol(expr: Option<&Expr>, expected: &str) -> bool {
    if let Some(Expr::Symbol(sym)) = expr {
        sym == expected
    } else {
        false
    }
}

fn expect_list<'a>(expr: &'a Expr, ctx: &str) -> Result<&'a Vec<Expr>> {
    if let Expr::List(list) = expr {
        Ok(list)
    } else {
        Err(validation(&format!("expected list in {}", ctx)))
    }
}

fn expect_symbol(expr: &Expr) -> Result<String> {
    match expr {
        Expr::Symbol(sym) => Ok(sym.clone()),
        _ => Err(validation("expected symbol")),
    }
}

fn expect_keyword(expr: &Expr) -> Result<String> {
    match expr {
        Expr::Keyword(kw) => Ok(kw.clone()),
        _ => Err(validation("expected keyword")),
    }
}

fn expect_string(expr: &Expr) -> Result<String> {
    match expr {
        Expr::String(s) => Ok(s.clone()),
        Expr::Symbol(sym) => Ok(sym.clone()),
        _ => Err(validation("expected string")),
    }
}

fn render_expr(expr: &Expr) -> String {
    match expr {
        Expr::Symbol(sym) => sym.clone(),
        Expr::Keyword(kw) => format!(":{}", kw),
        Expr::String(s) => format!("\"{}\"", s),
        Expr::Integer(i) => i.to_string(),
        Expr::Float(f) => f.to_string(),
        Expr::Boolean(b) => b.to_string(),
        Expr::List(items) => {
            let parts: Vec<String> = items.iter().map(render_expr).collect();
            format!("({})", parts.join(" "))
        }
    }
}

fn validation(msg: &str) -> WorkflowError {
    WorkflowError::Validation(msg.to_string())
}

fn append_instruction(target: &mut Vec<Instruction>, expr: &Expr) -> Result<()> {
    if let Expr::List(list) = expr {
        if matches_symbol(list.first(), "action") {
            for action in &list[1..] {
                target.push(Instruction::Action(parse_action(action)?));
            }
            return Ok(());
        }
    }
    target.push(parse_instruction(expr)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::interpreter::parser::parse_program;

    fn build(src: &str) -> ProgramIr {
        let program = parse_program(src).expect("parse");
        build_ir(&program).expect("build")
    }

    #[test]
    fn builds_roles_states_and_actions() {
        let ir = build("(workflow demo) (roles (planner :agent-kind \"claude\")) (state plan (action (send-prompt :agent planner :template \"hi\")))");
        assert_eq!(ir.name, "demo");
        assert_eq!(ir.roles.len(), 1);
        assert_eq!(ir.states.len(), 1);
        match &ir.states[0].body[0] {
            Instruction::Action(Action::SendPrompt { agent_role, template, .. }) => {
                assert_eq!(agent_role, "planner");
                assert_eq!(template, "hi");
            }
            other => panic!("unexpected instruction: {:?}", other),
        }
    }

    #[test]
    fn builds_branch_loop_and_transition() {
        let src = "
            (workflow demo)
            (state plan
              (loop (await (transcript-response :tag \"req\")))
              (branch
                (when (signal review/done) (goto complete))
                (otherwise (action (emit (log \"waiting\"))))))
            (state complete (terminal))
        ";
        let ir = build(src);
        assert_eq!(ir.states.len(), 2);
        assert!(matches!(ir.states[0].body[0], Instruction::Loop(_)));
        match &ir.states[0].body[1] {
            Instruction::Branch { arms, otherwise } => {
                assert_eq!(arms.len(), 1);
                assert!(otherwise.is_some());
            }
            other => panic!("expected branch, got {:?}", other),
        }
    }
}
