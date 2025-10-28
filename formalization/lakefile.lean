import Lake
open Lake DSL

package «duet-formalization» where
  moreLeanArgs := #[
    "-DautoImplicit=false",
    "-Dlinter.unusedVariables=false"
  ]

@[default_target]
lean_lib «DuetFormalization» where
  srcDir := "."
