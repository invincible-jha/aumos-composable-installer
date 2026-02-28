"""CLI command: aumos generate — generate Terraform/OpenTofu infrastructure configs.

Gap #12: Terraform/OpenTofu integration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

from aumos_common.observability import get_logger

from aumos_composable_installer.generator.terraform_generator import TerraformGenerator
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Generate infrastructure configuration files for AumOS cluster provisioning.",
    no_args_is_help=True,
)


@app.command("terraform")
def generate_terraform(
    provider: str = typer.Argument(..., help="Cloud provider: eks, gke, or aks."),
    cluster_name: str = typer.Option("aumos-cluster", "--cluster-name", "-c", help="Kubernetes cluster name."),
    output_dir: Path = typer.Option(
        Path("./aumos-infra"),
        "--output-dir",
        "-o",
        help="Directory to write generated .tf files.",
    ),
    kubernetes_version: str = typer.Option("1.29", "--k8s-version", help="Kubernetes version."),
    gpu_enabled: bool = typer.Option(False, "--gpu/--no-gpu", help="Include GPU node group configuration."),
    validate: bool = typer.Option(False, "--validate", help="Run terraform/tofu validate after generation."),
    iac_binary: Optional[str] = typer.Option(
        None,
        "--iac-binary",
        help="IaC binary to use: terraform or tofu (default from settings).",
    ),
) -> None:
    """Generate Terraform or OpenTofu configuration for AumOS cluster provisioning.

    Renders provider-specific Terraform templates (EKS, GKE, or AKS) into
    a local directory ready for terraform init and apply.

    Args:
        provider: Cloud provider to target: eks, gke, or aks.
        cluster_name: Kubernetes cluster name.
        output_dir: Directory to write generated .tf files.
        kubernetes_version: Kubernetes version string.
        gpu_enabled: Whether to include GPU node group configuration.
        validate: Run IaC validate after generation.
        iac_binary: Override IaC binary (terraform or tofu).
    """
    settings = Settings()
    binary = iac_binary or settings.iac_binary

    console.print(Panel(f"[bold cyan]Generating {provider.upper()} Terraform Configuration[/bold cyan]", expand=False))
    console.print(f"Binary: [bold]{binary}[/bold]")
    console.print(f"Output: {output_dir}")

    variables = {
        "cluster_name": cluster_name,
        "kubernetes_version": kubernetes_version,
        "gpu_enabled": gpu_enabled,
        "environment": "production",
    }

    generator = TerraformGenerator(provider=provider, iac_binary=binary)

    try:
        generated_files = generator.generate(output_dir=output_dir, variables=variables)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Generated {len(generated_files)} file(s):[/green]")
    for f in generated_files:
        console.print(f"  {f}")

    if validate:
        console.print("\nRunning IaC validation...")
        success = generator.validate(output_dir)
        if success:
            console.print("[green]Validation passed.[/green]")
        else:
            console.print("[red]Validation failed.[/red]")
            raise typer.Exit(code=1)

    console.print(f"\nNext steps:")
    console.print(f"  cd {output_dir}")
    console.print(f"  {binary} init")
    console.print(f"  {binary} plan")
    console.print(f"  {binary} apply")
