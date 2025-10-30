use std::collections::{BTreeMap, HashMap, HashSet};

use super::{
    Program, Result, WorkflowError,
    ast::Expr,
    protocol::ProgramRef,
    value::{parse_value_expr, parse_value_literal},
};
use crate::interpreter::ir::{
    Action, ActionTemplate, BranchArm, BranchArmTemplate, Condition, Function, Instruction,
    InstructionTemplate, ProgramIr, RoleBinding, State, WaitCondition, WaitConditionTemplate,
};

/// Build a typed IR from a parsed program.
pub fn build_ir(program: &Program) -> Result<ProgramIr> {
    let mut metadata = BTreeMap::new();
    let mut roles = Vec::new();
    let mut state_forms: Vec<Vec<Expr>> = Vec::new();
    let mut function_forms: Vec<Vec<Expr>> = Vec::new();

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
                state_forms.push(items.clone());
            }
            Expr::List(items) if matches_symbol(items.first(), "defn") => {
                function_forms.push(items.clone());
            }
            Expr::List(items) if matches_symbol(items.first(), "workflow") => {
                // name already inferred
                continue;
            }
            _ => {}
        }
    }

    // Build function prototypes so we know indices/arity before parsing bodies.
    let mut prototypes = HashMap::new();
    for (index, items) in function_forms.iter().enumerate() {
        if items.len() < 3 {
            return Err(validation("defn requires name, params, and body"));
        }
        let name = expect_symbol(&items[1])?;
        if prototypes.contains_key(&name) {
            return Err(validation("duplicate function name"));
        }
        let params_expr = expect_list(&items[2], "defn params")?;
        let params = params_expr
            .iter()
            .map(expect_symbol)
            .collect::<Result<Vec<_>>>()?;
        prototypes.insert(name.clone(), FunctionPrototype { index, params });
    }

    // Parse function bodies into templates.
    let mut functions = Vec::new();
    for items in function_forms {
        functions.push(parse_function(&items, &prototypes)?);
    }

    // Parse states now that functions are known.
    let mut states = Vec::new();
    for items in state_forms {
        states.push(parse_state(&items, &prototypes)?);
    }

    if states.is_empty() {
        return Err(validation("program must declare at least one state"));
    }

    Ok(ProgramIr {
        name: program.name.clone(),
        metadata,
        roles,
        states,
        functions,
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
    Ok(RoleBinding {
        name,
        properties: props,
    })
}

fn parse_state(items: &[Expr], prototypes: &HashMap<String, FunctionPrototype>) -> Result<State> {
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
                    entry.push(parse_action_literal(action)?);
                }
                continue;
            }
            if matches_symbol(list.first(), "terminal") {
                terminal = true;
                continue;
            }
        }
        append_instruction_literal(&mut body, form, prototypes)?;
    }

    Ok(State {
        name,
        entry,
        body,
        terminal,
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
        "observe handler must be a string program or (definition id)",
    ))
}

fn append_instruction_literal(
    target: &mut Vec<Instruction>,
    expr: &Expr,
    prototypes: &HashMap<String, FunctionPrototype>,
) -> Result<()> {
    if let Expr::List(list) = expr {
        if matches_symbol(list.first(), "action") {
            for action in &list[1..] {
                target.push(Instruction::Action(parse_action_literal(action)?));
            }
            return Ok(());
        }
    }
    target.push(parse_instruction_literal(expr, prototypes)?);
    Ok(())
}

fn parse_instruction_literal(
    expr: &Expr,
    prototypes: &HashMap<String, FunctionPrototype>,
) -> Result<Instruction> {
    if let Expr::List(list) = expr {
        if matches_symbol(list.first(), "await") {
            if list.len() != 2 {
                return Err(validation("await expects a single condition"));
            }
            return Ok(Instruction::Await(parse_wait(&list[1])?));
        }
        if matches_symbol(list.first(), "branch") {
            return parse_branch_literal(&list[1..], prototypes);
        }
        if matches_symbol(list.first(), "loop") {
            let mut loop_body = Vec::new();
            for item in &list[1..] {
                append_instruction_literal(&mut loop_body, item, prototypes)?;
            }
            return Ok(Instruction::Loop(loop_body));
        }
        if matches_symbol(list.first(), "goto") {
            if list.len() != 2 {
                return Err(validation("goto expects a state name"));
            }
            return Ok(Instruction::Transition(expect_symbol(&list[1])?));
        }
        if matches_symbol(list.first(), "call") {
            return parse_call_literal(list, prototypes);
        }
    }
    Ok(Instruction::Action(parse_action_literal(expr)?))
}

fn parse_branch_literal(
    arms: &[Expr],
    prototypes: &HashMap<String, FunctionPrototype>,
) -> Result<Instruction> {
    let mut parsed_arms = Vec::new();
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
                    append_instruction_literal(&mut body, instr, prototypes)?;
                }
                parsed_arms.push(BranchArm {
                    condition: cond,
                    body,
                });
            } else if matches_symbol(list.first(), "otherwise") {
                let mut body = Vec::new();
                for instr in &list[1..] {
                    append_instruction_literal(&mut body, instr, prototypes)?;
                }
                otherwise = Some(body);
            } else {
                return Err(validation("unknown branch arm"));
            }
        } else {
            return Err(validation("branch arms must be lists"));
        }
    }
    Ok(Instruction::Branch {
        arms: parsed_arms,
        otherwise,
    })
}

fn parse_call_literal(
    list: &[Expr],
    prototypes: &HashMap<String, FunctionPrototype>,
) -> Result<Instruction> {
    if list.len() < 2 {
        return Err(validation("call expects a function name"));
    }
    let func_name = expect_symbol(&list[1])?;
    let prototype = prototypes
        .get(&func_name)
        .ok_or_else(|| validation("unknown function"))?;
    if prototype.params.len() != list.len() - 2 {
        return Err(validation("call arity mismatch"));
    }
    let mut args = Vec::new();
    for expr in &list[2..] {
        args.push(parse_value_literal(expr)?);
    }
    Ok(Instruction::Call {
        function: prototype.index,
        args,
    })
}

fn parse_action_literal(expr: &Expr) -> Result<Action> {
    let list = expect_list(expr, "action")?;
    if list.is_empty() {
        return Err(validation("action list cannot be empty"));
    }
    let head = expect_symbol(&list[0])?;
    match head.as_str() {
        "invoke-tool" => {
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
                capability: capability
                    .ok_or_else(|| validation("invoke-tool requires :capability"))?,
                payload,
                tag,
            })
        }
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
        "log" => {
            if list.len() != 2 {
                return Err(validation("log expects a string message"));
            }
            Ok(Action::Log(expect_string(&list[1])?))
        }
        "send" => {
            let mut actor = None;
            let mut facet = None;
            let mut payload = None;
            let mut idx = 1;
            while idx < list.len() {
                let key = expect_keyword(&list[idx])?;
                idx += 1;
                match key.as_str() {
                    "actor" => {
                        actor = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    "facet" => {
                        facet = Some(expect_string(&list[idx])?);
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
        "observe" | "on" => {
            if list.len() != 3 {
                return Err(validation("on expects a pattern and handler"));
            }
            let wait = parse_wait(&list[1])?;
            let label = match wait {
                WaitCondition::Signal { label } => label,
                _ => {
                    return Err(validation(
                        "on currently only supports (signal <label>) patterns",
                    ));
                }
            };
            let handler = parse_program_ref_expr(&list[2])?;
            Ok(Action::Observe { label, handler })
        }
        "spawn" | "spawn-facet" => {
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
        "stop" | "stop-facet" => {
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
        other => Err(validation(&format!("unknown action form: {}", other))),
    }
}

fn parse_function(
    items: &[Expr],
    prototypes: &HashMap<String, FunctionPrototype>,
) -> Result<Function> {
    if items.len() < 3 {
        return Err(validation("defn requires name, params, and body"));
    }
    let name = expect_symbol(&items[1])?;
    let prototype = prototypes
        .get(&name)
        .ok_or_else(|| validation("unknown function prototype"))?;
    let param_set = prototype.params.iter().cloned().collect::<HashSet<_>>();

    let mut body = Vec::new();
    for expr in &items[3..] {
        append_instruction_template(&mut body, expr, prototypes, &param_set)?;
    }

    Ok(Function {
        name,
        params: prototype.params.clone(),
        body,
    })
}

fn append_instruction_template(
    target: &mut Vec<InstructionTemplate>,
    expr: &Expr,
    prototypes: &HashMap<String, FunctionPrototype>,
    params: &HashSet<String>,
) -> Result<()> {
    if let Expr::List(list) = expr {
        if matches_symbol(list.first(), "action") {
            for action in &list[1..] {
                target.push(InstructionTemplate::Action(parse_action_template(
                    action, params,
                )?));
            }
            return Ok(());
        }
    }
    target.push(parse_instruction_template(expr, prototypes, params)?);
    Ok(())
}

fn parse_instruction_template(
    expr: &Expr,
    prototypes: &HashMap<String, FunctionPrototype>,
    params: &HashSet<String>,
) -> Result<InstructionTemplate> {
    if let Expr::List(list) = expr {
        if matches_symbol(list.first(), "await") {
            if list.len() != 2 {
                return Err(validation("await expects a single condition"));
            }
            return Ok(InstructionTemplate::Await(parse_wait_template(
                &list[1], params,
            )?));
        }
        if matches_symbol(list.first(), "branch") {
            return parse_branch_template(&list[1..], prototypes, params);
        }
        if matches_symbol(list.first(), "loop") {
            let mut loop_body = Vec::new();
            for item in &list[1..] {
                append_instruction_template(&mut loop_body, item, prototypes, params)?;
            }
            return Ok(InstructionTemplate::Loop(loop_body));
        }
        if matches_symbol(list.first(), "goto") {
            if list.len() != 2 {
                return Err(validation("goto expects a state name"));
            }
            return Ok(InstructionTemplate::Transition(expect_symbol(&list[1])?));
        }
        if matches_symbol(list.first(), "call") {
            return parse_call_template(list, prototypes, params);
        }
    }
    Ok(InstructionTemplate::Action(parse_action_template(
        expr, params,
    )?))
}

fn parse_branch_template(
    arms: &[Expr],
    prototypes: &HashMap<String, FunctionPrototype>,
    params: &HashSet<String>,
) -> Result<InstructionTemplate> {
    let mut parsed_arms = Vec::new();
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
                    append_instruction_template(&mut body, instr, prototypes, params)?;
                }
                parsed_arms.push(BranchArmTemplate {
                    condition: cond,
                    body,
                });
            } else if matches_symbol(list.first(), "otherwise") {
                let mut body = Vec::new();
                for instr in &list[1..] {
                    append_instruction_template(&mut body, instr, prototypes, params)?;
                }
                otherwise = Some(body);
            } else {
                return Err(validation("unknown branch arm"));
            }
        } else {
            return Err(validation("branch arms must be lists"));
        }
    }
    Ok(InstructionTemplate::Branch {
        arms: parsed_arms,
        otherwise,
    })
}

fn parse_call_template(
    list: &[Expr],
    prototypes: &HashMap<String, FunctionPrototype>,
    params: &HashSet<String>,
) -> Result<InstructionTemplate> {
    if list.len() < 2 {
        return Err(validation("call expects a function name"));
    }
    let func_name = expect_symbol(&list[1])?;
    let prototype = prototypes
        .get(&func_name)
        .ok_or_else(|| validation("unknown function"))?;
    if prototype.params.len() != list.len() - 2 {
        return Err(validation("call arity mismatch"));
    }

    let mut args = Vec::new();
    for expr in &list[2..] {
        args.push(parse_value_expr(expr, params)?);
    }

    Ok(InstructionTemplate::Call {
        function: prototype.index,
        args,
    })
}

fn parse_action_template(expr: &Expr, params: &HashSet<String>) -> Result<ActionTemplate> {
    let list = expect_list(expr, "action")?;
    if list.is_empty() {
        return Err(validation("action list cannot be empty"));
    }
    let head = expect_symbol(&list[0])?;
    match head.as_str() {
        "invoke-tool" => {
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
                        payload = Some(parse_value_expr(&list[idx], params)?);
                        idx += 1;
                    }
                    "tag" => {
                        tag = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    _ => return Err(validation("unknown invoke-tool argument")),
                }
            }
            Ok(ActionTemplate::InvokeTool {
                role: role.ok_or_else(|| validation("invoke-tool requires :role"))?,
                capability: capability
                    .ok_or_else(|| validation("invoke-tool requires :capability"))?,
                payload,
                tag,
            })
        }
        "assert" => {
            if list.len() != 2 {
                return Err(validation("assert expects a value"));
            }
            Ok(ActionTemplate::Assert(parse_value_expr(&list[1], params)?))
        }
        "retract" => {
            if list.len() != 2 {
                return Err(validation("retract expects a value"));
            }
            Ok(ActionTemplate::Retract(parse_value_expr(&list[1], params)?))
        }
        "log" => {
            if list.len() != 2 {
                return Err(validation("log expects a string message"));
            }
            Ok(ActionTemplate::Log(expect_string(&list[1])?))
        }
        "send" => {
            let mut actor = None;
            let mut facet = None;
            let mut payload = None;
            let mut idx = 1;
            while idx < list.len() {
                let key = expect_keyword(&list[idx])?;
                idx += 1;
                match key.as_str() {
                    "actor" => {
                        actor = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    "facet" => {
                        facet = Some(expect_string(&list[idx])?);
                        idx += 1;
                    }
                    "value" => {
                        payload = Some(parse_value_expr(&list[idx], params)?);
                        idx += 1;
                    }
                    _ => return Err(validation("unknown send argument")),
                }
            }

            Ok(ActionTemplate::Send {
                actor: actor.ok_or_else(|| validation("send requires :actor"))?,
                facet: facet.ok_or_else(|| validation("send requires :facet"))?,
                payload: payload.ok_or_else(|| validation("send requires :value"))?,
            })
        }
        "observe" | "on" => {
            if list.len() != 3 {
                return Err(validation("on expects a pattern and handler"));
            }
            let wait = parse_wait(&list[1])?;
            let label = match wait {
                WaitCondition::Signal { label } => label,
                _ => {
                    return Err(validation(
                        "on currently only supports (signal <label>) patterns",
                    ));
                }
            };
            let handler = parse_program_ref_expr(&list[2])?;
            Ok(ActionTemplate::Observe { label, handler })
        }
        "spawn" | "spawn-facet" => {
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

            Ok(ActionTemplate::Spawn { parent })
        }
        "stop" | "stop-facet" => {
            if list.len() != 3 {
                return Err(validation("stop expects :facet <uuid>"));
            }
            if !matches!(list[1], Expr::Keyword(_)) || expect_keyword(&list[1])? != "facet" {
                return Err(validation("stop expects :facet <uuid>"));
            }
            Ok(ActionTemplate::Stop {
                facet: expect_string(&list[2])?,
            })
        }
        other => Err(validation(&format!("unknown action form: {}", other))),
    }
}

fn parse_wait(expr: &Expr) -> Result<WaitCondition> {
    let list = expect_list(expr, "wait")?;
    if list.is_empty() {
        return Err(validation("wait condition cannot be empty"));
    }
    let head = expect_symbol(&list[0])?;
    match head.as_str() {
        "record" => {
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
        "signal" => {
            if list.len() < 2 {
                return Err(validation("signal requires a label"));
            }
            Ok(WaitCondition::Signal {
                label: expect_symbol(&list[1])?,
            })
        }
        "tool-result" => {
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
        _ => Err(validation("unknown wait condition")),
    }
}

fn parse_wait_template(expr: &Expr, params: &HashSet<String>) -> Result<WaitConditionTemplate> {
    let list = expect_list(expr, "wait")?;
    if list.is_empty() {
        return Err(validation("wait condition cannot be empty"));
    }
    let head = expect_symbol(&list[0])?;
    match head.as_str() {
        "record" => {
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
                        value = Some(parse_value_expr(&list[idx], params)?);
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

            Ok(WaitConditionTemplate::RecordFieldEq {
                label,
                field: field as usize,
                value,
            })
        }
        "signal" => {
            if list.len() < 2 {
                return Err(validation("signal requires a label"));
            }
            Ok(WaitConditionTemplate::Signal {
                label: expect_symbol(&list[1])?,
            })
        }
        "tool-result" => {
            if list.len() == 2 && !matches!(list[1], Expr::Keyword(_)) {
                return Ok(WaitConditionTemplate::ToolResult {
                    tag: parse_value_expr(&list[1], params)?,
                });
            }

            let mut tag = None;
            let mut idx = 1;
            while idx < list.len() {
                let key = expect_keyword(&list[idx])?;
                idx += 1;
                match key.as_str() {
                    "tag" => {
                        tag = Some(parse_value_expr(&list[idx], params)?);
                        idx += 1;
                    }
                    _ => return Err(validation("unknown tool-result wait argument")),
                }
            }

            let tag = tag.ok_or_else(|| validation("tool-result wait requires :tag"))?;
            Ok(WaitConditionTemplate::ToolResult { tag })
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

struct FunctionPrototype {
    index: usize,
    params: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::interpreter::protocol::{RUN_MESSAGE_LABEL, TOOL_REQUEST_RECORD_LABEL};
    use crate::interpreter::{Value, entity::InterpreterEntity, parser::parse_program};
    use crate::runtime::actor::{Activation, Actor, Entity};
    use crate::runtime::turn::{ActorId, TurnOutput};
    use crate::util::io_value::record_with_label;
    use preserves::IOValue;
    use uuid::Uuid;

    fn build(src: &str) -> ProgramIr {
        let program = parse_program(src).expect("parse");
        build_ir(&program).expect("build")
    }

    #[test]
    fn builds_roles_states_and_actions() {
        let ir = build(
            "(workflow demo)
             (roles (planner :agent-kind \"claude\"))
             (state plan (action (log \"hi\")))",
        );
        assert_eq!(ir.name, "demo");
        assert_eq!(ir.roles.len(), 1);
        assert_eq!(ir.states.len(), 1);
        assert!(ir.functions.is_empty());
        match &ir.states[0].body[0] {
            Instruction::Action(Action::Log(message)) => assert_eq!(message, "hi"),
            other => panic!("unexpected instruction: {:?}", other),
        }
    }

    #[test]
    fn builds_branch_loop_and_transition() {
        let src = "
            (workflow demo)
            (state plan
              (loop (await (record agent-response :field 0 :equals \"req\")))
              (branch
                (when (signal review/done) (goto complete))
                (otherwise
                  (action (log \"waiting\")))))
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

    #[test]
    fn builds_invoke_tool_action() {
        let capability_id = Uuid::new_v4();
        let src = format!(
            "(workflow demo)
               (roles (workspace :capability \"{capability}\"))
               (state start
                 (action (invoke-tool :role workspace :capability \"capability\" :payload (record request \"payload\") :tag tool-req))
                 (terminal))",
            capability = capability_id
        );

        let ir = build(&src);
        assert_eq!(ir.states.len(), 1);
        match &ir.states[0].body[0] {
            Instruction::Action(Action::InvokeTool {
                role,
                capability,
                payload,
                tag,
            }) => {
                assert_eq!(role, "workspace");
                assert_eq!(capability, "capability");
                assert_eq!(tag.as_deref(), Some("tool-req"));
                let payload = payload.as_ref().expect("payload should be present");
                match payload {
                    Value::Record { label, fields } => {
                        assert_eq!(label, "request");
                        assert_eq!(fields.len(), 1);
                        assert_eq!(fields[0], Value::String("payload".into()));
                    }
                    other => panic!("unexpected payload value: {:?}", other),
                }
            }
            other => panic!("expected invoke-tool action, got {:?}", other),
        }

        let entity = InterpreterEntity::default();
        let actor = Actor::new(ActorId::new());
        let mut activation = Activation::new(actor.id.clone(), actor.root_facet.clone(), None);

        let run_payload =
            IOValue::record(IOValue::symbol(RUN_MESSAGE_LABEL), vec![IOValue::new(src)]);

        entity.on_message(&mut activation, &run_payload).unwrap();

        let tool_request = activation
            .assertions_added
            .iter()
            .find_map(|(_, value)| {
                value
                    .label()
                    .as_symbol()
                    .filter(|sym| sym.as_ref() == TOOL_REQUEST_RECORD_LABEL)
                    .map(|_| value.clone())
            })
            .expect("tool request record should be asserted");

        let request_view = record_with_label(&tool_request, TOOL_REQUEST_RECORD_LABEL).unwrap();
        let instance_id = request_view.field_string(0).expect("instance id");
        let tag_value = request_view.field_string(1).expect("tag");

        let invoke = activation
            .outputs
            .iter()
            .find_map(|output| {
                if let TurnOutput::CapabilityInvoke {
                    capability,
                    payload,
                    completion,
                } = output
                {
                    Some((capability, payload, completion))
                } else {
                    None
                }
            })
            .expect("capability invocation output");

        assert_eq!(*invoke.0, capability_id);
        let payload_view = record_with_label(&invoke.1, "request").expect("payload record");
        assert_eq!(payload_view.field_string(0).as_deref(), Some("payload"));

        assert_eq!(invoke.2.instance_id, instance_id);
        assert_eq!(invoke.2.tag, tag_value);
        assert_eq!(invoke.2.role, "workspace");
        assert_eq!(invoke.2.capability_alias, "capability");
        assert_eq!(invoke.2.origin_actor, activation.actor_id);
    }

    #[test]
    fn parses_tool_result_wait() {
        let src = "
            (workflow demo)
            (state wait
              (await (tool-result :tag \"tool-123\"))
              (terminal))
        ";
        let ir = build(src);
        assert_eq!(ir.states.len(), 1);
        match &ir.states[0].body[0] {
            Instruction::Await(WaitCondition::ToolResult { tag }) => {
                assert_eq!(tag, "tool-123");
            }
            other => panic!("expected tool-result wait, got {:?}", other),
        }
    }

    #[test]
    fn produces_call_instruction_for_functions() {
        let src = "
            (workflow demo)
            (defn greet (person)
              (action (assert (record greeting person))))
            (state start
              (call greet \"Alice\")
              (call greet \"Bob\")
              (terminal))
        ";
        let ir = build(src);
        assert_eq!(ir.functions.len(), 1);
        assert_eq!(ir.functions[0].params, vec!["person"]);
        assert_eq!(ir.states.len(), 1);
        assert!(matches!(
            ir.states[0].body[0],
            Instruction::Call { function: 0, .. }
        ));
        assert!(matches!(
            ir.states[0].body[1],
            Instruction::Call { function: 0, .. }
        ));
    }
}
