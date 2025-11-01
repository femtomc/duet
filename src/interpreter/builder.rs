use std::collections::BTreeMap;

use super::{
    Program, Result, WorkflowError, ast::Expr, protocol::ProgramRef, value::parse_value_literal,
};
use crate::interpreter::ir::{Action, Command, ProgramIr, RoleBinding, State, WaitCondition};

/// Build a minimal IR from a parsed program.
pub fn build_ir(program: &Program) -> Result<ProgramIr> {
    let mut metadata = BTreeMap::new();
    let mut roles = Vec::new();
    let mut states = Vec::new();

    for form in &program.forms {
        match form {
            Expr::List(items) if matches_symbol(items.first(), "metadata") => {
                parse_metadata(items, &mut metadata)?;
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
                // Program name already captured when parsing.
                continue;
            }
            other => {
                return Err(validation(&format!(
                    "unsupported top-level form: {:?}",
                    other
                )));
            }
        }
    }

    if states.is_empty() {
        return Err(validation(&format!(
            "program '{}' must declare at least one (state ...) form",
            program.name
        )));
    }

    Ok(ProgramIr {
        name: program.name.clone(),
        metadata,
        roles,
        states,
    })
}

fn parse_metadata(items: &[Expr], metadata: &mut BTreeMap<String, String>) -> Result<()> {
    for entry in &items[1..] {
        if let Expr::List(pair) = entry {
            if pair.len() != 2 {
                return Err(validation("metadata entries must be (key value)"));
            }
            let key = expect_string(&pair[0])?;
            let value = expect_string(&pair[1])?;
            metadata.insert(key, value);
        } else {
            return Err(validation("metadata expects (key value) pairs"));
        }
    }
    Ok(())
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
    Ok(RoleBinding {
        name,
        properties: props,
    })
}

fn parse_state(items: &[Expr]) -> Result<State> {
    if items.len() < 2 {
        return Err(validation("state requires a name"));
    }
    let name = expect_symbol(&items[1])?;
    let mut commands = Vec::new();
    let mut terminal = false;

    for form in &items[2..] {
        let list = expect_list(form, "state command")?;
        if list.is_empty() {
            return Err(validation("state command cannot be empty"));
        }
        let head = expect_symbol(&list[0])?;
        match head.as_str() {
            "emit" => {
                if list.len() != 2 {
                    return Err(validation("emit expects a single action form"));
                }
                commands.push(Command::Emit(parse_action(&list[1])?));
            }
            "await" => {
                if list.len() != 2 {
                    return Err(validation("await expects a single condition"));
                }
                commands.push(Command::Await(parse_wait(&list[1])?));
            }
            "transition" => {
                if list.len() != 2 {
                    return Err(validation("transition expects a state name"));
                }
                commands.push(Command::Transition(expect_symbol(&list[1])?));
            }
            "terminal" => {
                if list.len() != 1 {
                    return Err(validation("terminal does not take arguments"));
                }
                terminal = true;
            }
            other => {
                return Err(validation(&format!("unknown command '{}'", other)));
            }
        }
    }

    Ok(State {
        name,
        commands,
        terminal,
    })
}

fn parse_action(expr: &Expr) -> Result<Action> {
    let list = expect_list(expr, "action")?;
    if list.is_empty() {
        return Err(validation("action cannot be empty"));
    }
    let head = expect_symbol(&list[0])?;
    match head.as_str() {
        "invoke-tool" => parse_invoke_tool_action(list),
        "assert" => {
            if list.len() != 2 {
                return Err(validation("assert expects a value"));
            }
            Ok(Action::Assert(parse_value_literal(&list[1])?))
        }
        "retract" => {
            if list.len() != 2 {
                return Err(validation("retract expects a value"));
            }
            Ok(Action::Retract(parse_value_literal(&list[1])?))
        }
        "register-pattern" => parse_register_pattern_action(list),
        "unregister-pattern" => parse_unregister_pattern_action(list),
        "detach-entity" => parse_detach_entity_action(list),
        "log" => {
            if list.len() != 2 {
                return Err(validation("log expects a string message"));
            }
            Ok(Action::Log(expect_string(&list[1])?))
        }
        "send" => parse_send_action(list),
        "observe" | "on" => parse_observe_action(list),
        "spawn" | "spawn-facet" => parse_spawn_action(list),
        "spawn-entity" => parse_spawn_entity_action(list),
        "attach-entity" => parse_attach_entity_action(list),
        "generate-request-id" => parse_generate_request_id_action(list),
        "stop" | "stop-facet" => parse_stop_action(list),
        other => Err(validation(&format!("unknown action '{}'", other))),
    }
}

fn parse_wait(expr: &Expr) -> Result<WaitCondition> {
    match expr {
        Expr::Symbol(sym) => Ok(WaitCondition::Signal { label: sym.clone() }),
        Expr::List(list) => {
            if list.is_empty() {
                return Err(validation("wait condition cannot be empty"));
            }
            let head = expect_symbol(&list[0])?;
            match head.as_str() {
                "record" => parse_record_wait(list),
                "signal" => {
                    if list.len() != 2 {
                        return Err(validation("signal wait expects a label"));
                    }
                    Ok(WaitCondition::Signal {
                        label: expect_symbol(&list[1])?,
                    })
                }
                "tool-result" => parse_tool_result_wait(list),
                "user-input" => parse_user_input_wait(list),
                other => Err(validation(&format!("unknown wait condition '{}'", other))),
            }
        }
        _ => Err(validation("wait condition must be a symbol or list")),
    }
}

fn parse_record_wait(list: &[Expr]) -> Result<WaitCondition> {
    if list.len() < 2 {
        return Err(validation("record wait requires label"));
    }
    let label = expect_symbol(&list[1])?;
    let mut field = None;
    let mut value = None;
    let mut idx = 2;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "field" => {
                field = Some(expect_integer(&list[idx])?);
                idx += 1;
            }
            "equals" => {
                value = Some(parse_value_literal(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown record wait argument")),
        }
    }

    let field = field.ok_or_else(|| validation("record wait requires :field"))?;
    let value = value.ok_or_else(|| validation("record wait requires :equals"))?;
    if field < 0 {
        return Err(validation("record wait :field must be non-negative"));
    }
    Ok(WaitCondition::RecordFieldEq {
        label,
        field: field as usize,
        value,
    })
}

fn parse_tool_result_wait(list: &[Expr]) -> Result<WaitCondition> {
    if list.len() == 2 && !matches!(list[1], Expr::Keyword(_)) {
        return Ok(WaitCondition::ToolResult {
            tag: expect_string(&list[1])?,
        });
    }
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
            _ => return Err(validation("unknown tool-result wait argument")),
        }
    }
    let tag = tag.ok_or_else(|| validation("tool-result wait requires :tag"))?;
    Ok(WaitCondition::ToolResult { tag })
}

fn parse_user_input_wait(list: &[Expr]) -> Result<WaitCondition> {
    let mut prompt = None;
    let mut tag = None;

    if list.len() == 2 && !matches!(list[1], Expr::Keyword(_)) {
        prompt = Some(parse_value_literal(&list[1])?);
    } else {
        let mut idx = 1;
        while idx < list.len() {
            let key = expect_keyword(&list[idx])?;
            idx += 1;
            match key.as_str() {
                "prompt" => {
                    prompt = Some(parse_value_literal(&list[idx])?);
                    idx += 1;
                }
                "tag" => {
                    tag = Some(expect_string(&list[idx])?);
                    idx += 1;
                }
                _ => return Err(validation("unknown user-input wait argument")),
            }
        }
    }

    let prompt = prompt.ok_or_else(|| validation("user-input wait requires a :prompt value"))?;
    Ok(WaitCondition::UserInput {
        prompt,
        tag,
        request_id: None,
    })
}

fn parse_invoke_tool_action(list: &[Expr]) -> Result<Action> {
    let mut role = None;
    let mut capability = None;
    let mut payload = None;
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
            "payload" => {
                payload = Some(parse_value_literal(&list[idx])?);
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
        payload,
        tag,
    })
}

fn parse_register_pattern_action(list: &[Expr]) -> Result<Action> {
    let mut role = None;
    let mut pattern = None;
    let mut property = None;
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "role" => {
                role = Some(expect_symbol(&list[idx])?);
                idx += 1;
            }
            "pattern" => {
                pattern = Some(parse_value_literal(&list[idx])?);
                idx += 1;
            }
            "property" => {
                property = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown register-pattern argument")),
        }
    }
    Ok(Action::RegisterPattern {
        role: role.ok_or_else(|| validation("register-pattern requires :role"))?,
        pattern: pattern.ok_or_else(|| validation("register-pattern requires :pattern"))?,
        property,
    })
}

fn parse_unregister_pattern_action(list: &[Expr]) -> Result<Action> {
    let mut role = None;
    let mut pattern = None;
    let mut property = None;
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "role" => {
                role = Some(expect_symbol(&list[idx])?);
                idx += 1;
            }
            "pattern" => {
                pattern = Some(parse_value_literal(&list[idx])?);
                idx += 1;
            }
            "property" => {
                property = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown unregister-pattern argument")),
        }
    }
    Ok(Action::UnregisterPattern {
        role: role.ok_or_else(|| validation("unregister-pattern requires :role"))?,
        pattern,
        property,
    })
}

fn parse_detach_entity_action(list: &[Expr]) -> Result<Action> {
    let mut role = None;
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "role" => {
                role = Some(expect_symbol(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown detach-entity argument")),
        }
    }
    Ok(Action::DetachEntity {
        role: role.ok_or_else(|| validation("detach-entity requires :role"))?,
    })
}

fn parse_send_action(list: &[Expr]) -> Result<Action> {
    let mut actor = None;
    let mut facet = None;
    let mut payload = None;
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "actor" => {
                actor = Some(parse_value_literal(&list[idx])?);
                idx += 1;
            }
            "facet" => {
                facet = Some(parse_value_literal(&list[idx])?);
                idx += 1;
            }
            "value" => {
                payload = Some(parse_value_literal(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown send argument")),
        }
    }
    Ok(Action::Send {
        actor: actor.ok_or_else(|| validation("send requires :actor"))?,
        facet: facet.ok_or_else(|| validation("send requires :facet"))?,
        payload: payload.ok_or_else(|| validation("send requires :value"))?,
    })
}

fn parse_observe_action(list: &[Expr]) -> Result<Action> {
    if list.len() != 3 {
        return Err(validation("observe expects a pattern and handler"));
    }
    let wait = parse_wait(&list[1])?;
    let label = match wait {
        WaitCondition::Signal { label } => label,
        _ => {
            return Err(validation(
                "observe currently only supports (signal <label>) patterns",
            ));
        }
    };
    let handler = parse_program_ref_expr(&list[2])?;
    Ok(Action::Observe { label, handler })
}

fn parse_spawn_action(list: &[Expr]) -> Result<Action> {
    let mut parent = None;
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "parent" => {
                parent = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown spawn argument")),
        }
    }
    Ok(Action::Spawn { parent })
}

fn parse_spawn_entity_action(list: &[Expr]) -> Result<Action> {
    let mut role = None;
    let mut entity_type = None;
    let mut agent_kind = None;
    let mut config = None;
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "role" => {
                role = Some(expect_symbol(&list[idx])?);
                idx += 1;
            }
            "entity-type" | "type" => {
                entity_type = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            "agent-kind" => {
                agent_kind = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            "config" => {
                config = Some(parse_value_literal(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown spawn-entity argument")),
        }
    }
    Ok(Action::SpawnEntity {
        role: role.ok_or_else(|| validation("spawn-entity requires :role"))?,
        entity_type,
        agent_kind,
        config,
    })
}

fn parse_attach_entity_action(list: &[Expr]) -> Result<Action> {
    let mut role = None;
    let mut facet = None;
    let mut entity_type = None;
    let mut agent_kind = None;
    let mut config = None;
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "role" => {
                role = Some(expect_symbol(&list[idx])?);
                idx += 1;
            }
            "facet" => {
                facet = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            "entity-type" | "type" => {
                entity_type = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            "agent-kind" => {
                agent_kind = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            "config" => {
                config = Some(parse_value_literal(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown attach-entity argument")),
        }
    }
    Ok(Action::AttachEntity {
        role: role.ok_or_else(|| validation("attach-entity requires :role"))?,
        facet,
        entity_type,
        agent_kind,
        config,
    })
}

fn parse_generate_request_id_action(list: &[Expr]) -> Result<Action> {
    let mut role = None;
    let mut property = None;
    let mut idx = 1;
    while idx < list.len() {
        let key = expect_keyword(&list[idx])?;
        idx += 1;
        match key.as_str() {
            "role" => {
                role = Some(expect_symbol(&list[idx])?);
                idx += 1;
            }
            "store" | "property" => {
                property = Some(expect_string(&list[idx])?);
                idx += 1;
            }
            _ => return Err(validation("unknown generate-request-id argument")),
        }
    }
    Ok(Action::GenerateRequestId {
        role: role.ok_or_else(|| validation("generate-request-id requires :role"))?,
        property: property.ok_or_else(|| validation("generate-request-id requires :store"))?,
    })
}

fn parse_stop_action(list: &[Expr]) -> Result<Action> {
    if list.len() != 3 {
        return Err(validation("stop expects :facet <uuid>"));
    }
    if !matches!(list[1], Expr::Keyword(_)) || expect_keyword(&list[1])? != "facet" {
        return Err(validation("stop expects :facet <uuid>"));
    }
    Ok(Action::Stop {
        facet: expect_string(&list[2])?,
    })
}

fn parse_program_ref_expr(expr: &Expr) -> Result<ProgramRef> {
    if let Expr::List(items) = expr {
        if matches_symbol(items.first(), "definition") {
            if items.len() != 2 {
                return Err(validation("definition reference expects an id"));
            }
            let id = expect_string(&items[1])?;
            return Ok(ProgramRef::Definition(id));
        }
    }

    if let Expr::String(text) = expr {
        return Ok(ProgramRef::Inline(text.clone()));
    }

    Err(validation(
        "observer handler must be a string program or (definition id)",
    ))
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

fn expect_integer(expr: &Expr) -> Result<i64> {
    match expr {
        Expr::Integer(num) => Ok(*num),
        _ => Err(validation("expected integer literal")),
    }
}

fn expect_string(expr: &Expr) -> Result<String> {
    match expr {
        Expr::String(s) => Ok(s.clone()),
        Expr::Symbol(sym) => Ok(sym.clone()),
        _ => Err(validation("expected string")),
    }
}

fn validation(msg: &str) -> WorkflowError {
    WorkflowError::Validation(msg.to_string())
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
    fn builds_simple_state_machine() {
        let ir = build(
            "(workflow demo)
             (roles (planner :agent-kind \"claude\"))
             (state plan
               (emit (log \"hi\"))
               (await (signal ready))
               (transition done))
             (state done
               (terminal))",
        );
        assert_eq!(ir.states.len(), 2);
        assert!(matches!(ir.states[0].commands[0], Command::Emit(_)));
        assert!(matches!(ir.states[0].commands[1], Command::Await(_)));
        assert!(matches!(ir.states[0].commands[2], Command::Transition(_)));
        assert!(ir.states[1].terminal);
    }
}
