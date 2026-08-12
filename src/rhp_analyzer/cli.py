from __future__ import annotations

import argparse
import json
from pathlib import Path

from .benchmark import inspect_document, run_benchmark


def _pdf_paths(values: list[str]) -> list[Path]:
    paths = [Path(value).resolve() for value in values]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit(f"These PDF files do not exist: {', '.join(missing)}")
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rhp-analyzer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Show the PDF sections and size estimates.",
    )
    inspect_parser.add_argument("pdfs", nargs="+")

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Test the evidence extraction.",
    )
    benchmark_parser.add_argument("pdfs", nargs="+")
    benchmark_parser.add_argument("--model", default="gpt-5.6-terra")
    benchmark_parser.add_argument("--retries", type=int, default=2)
    benchmark_parser.add_argument(
        "--strategy",
        choices=("text_sections", "whole_pdf", "both"),
        default="text_sections",
    )
    benchmark_parser.add_argument(
        "--sections",
        default="offer,financial_summary,objects,business,risks,basis_for_price,litigation",
        help="Use section names that commas separate. Use 'all' for all sections.",
    )
    benchmark_parser.add_argument(
        "--output-dir", type=Path, default=Path("benchmark-results")
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    pdfs = _pdf_paths(args.pdfs)
    if args.command == "inspect":
        print(
            json.dumps(
                [inspect_document(path) for path in pdfs], indent=2, ensure_ascii=False
            )
        )
        return

    selected_sections = (
        None if args.sections == "all" else set(args.sections.split(","))
    )
    try:
        result_path = run_benchmark(
            pdf_paths=pdfs,
            model=args.model,
            retries=args.retries,
            selected_sections=selected_sections,
            strategy=args.strategy,
            output_dir=args.output_dir.resolve(),
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(result_path)


if __name__ == "__main__":
    main()
