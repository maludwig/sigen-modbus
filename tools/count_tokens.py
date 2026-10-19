"""Count a file's tokens as a Claude model sees them, with the Anthropic API's token counter (it doesn't run the
model), or list the models the API offers.

    python tools/count_tokens.py --input-file scratch/MEGA_REF.md --model claude-opus-5-5
    python tools/count_tokens.py --input-file scratch/BOT_README.md scratch/HANDOFF.md   # several: one line each
    python tools/count_tokens.py --list-models

Needs the anthropic package (a dev dependency) and an API credential: ANTHROPIC_API_KEY, or a profile from
`ant auth login`. Each file is counted as one user message, so a count includes that message's few tokens of framing.
Counts are per model (tokenizers differ), so pass the model you'll use. Not tiktoken: that's OpenAI's tokenizer,
and it undercounts Claude's tokens, most on code.
"""

import argparse
import sys
from pathlib import Path

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"


def count_file(client: anthropic.Anthropic, path: Path, model: str) -> int:
    """`path`'s text (UTF-8) as one user message, counted in `model`'s tokens."""
    text = path.read_text(encoding="utf-8-sig")
    return client.messages.count_tokens(model=model, messages=[{"role": "user", "content": text}]).input_tokens


def list_models(client: anthropic.Anthropic) -> None:
    """Print each model the API offers, newest first: its ID, name, context window and output limit."""
    for m in client.models.list():
        window = getattr(m, "max_input_tokens", None)
        output = getattr(m, "max_tokens", None)
        sizes = f"{window:>9,} in, {output:>7,} out" if window and output else ""
        print(f"{m.id:28} {m.display_name:24} {sizes}")


def main(argv: list[str] | None = None) -> int:
    """The command line: count each --input-file, or --list-models. Exit 2 on a bad argument or an unreadable file,
    1 on an API error (with what went wrong)."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--input-file", nargs="+", type=Path, metavar="FILE", help="file(s) to count")
    what.add_argument("--list-models", action="store_true", help="list the models the API offers, then stop")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"the model whose tokens to count (default: {DEFAULT_MODEL})")
    args = p.parse_args(argv)
    client = anthropic.Anthropic()
    try:
        if args.list_models:
            list_models(client)
            return 0
        total = 0
        for path in args.input_file:
            try:
                tokens = count_file(client, path, args.model)
            except OSError as e:
                print(f"error: can't read {path}: {e}", file=sys.stderr)
                return 2
            total += tokens
            print(f"{tokens:>10,}  {path}")
        if len(args.input_file) > 1:
            print(f"{total:>10,}  total ({args.model})")
        else:
            print(f"{'':>10}  ({args.model})")
        return 0
    except anthropic.AuthenticationError:
        print("error: the API rejected the credential (ANTHROPIC_API_KEY, or `ant auth login`)", file=sys.stderr)
    except anthropic.NotFoundError:
        print(f"error: no model {args.model!r}; see --list-models", file=sys.stderr)
    except anthropic.RateLimitError:
        print("error: rate limited; try again in a minute", file=sys.stderr)
    except anthropic.APIStatusError as e:
        print(f"error: the API answered HTTP {e.status_code}: {e.message}", file=sys.stderr)
    except anthropic.APIConnectionError as e:
        print(f"error: couldn't reach the API: {e}", file=sys.stderr)
    except anthropic.AnthropicError as e:
        print(f"error: {e}", file=sys.stderr)
    except TypeError as e:  # the SDK's way of saying there's no credential at all
        if "Could not resolve authentication method" not in str(e):
            raise
        print("error: no API credential: set ANTHROPIC_API_KEY, or run `ant auth login`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
