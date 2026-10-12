# Diagnostic evaluations

This is the one guide to Tracaus's evaluation suite. The suite is `diagnostics-v1`. It holds 16 synthetic cases in
`cases.json`. Each case has a prompt, the fake evidence the investigation tools would return, a reference answer
and the grade that answer should get (`VALID` or `REJECTED`). The evidence is fake logs and metric descriptors in the
real tool response format. It holds no customer data or credentials. The diagnosis checker (`diagnosis-v1`) and the
redaction rules (`redaction-v1`) have their own versions, and every result records them with the hashes of the
fixtures and of the prompt (`agent-instruction.txt`).

## What the 16 cases test

- **Ten good reference answers that must be accepted.** No traffic, collector or transport failure, a correlated
  hang, resource pressure, a known service failure, contradictory evidence, insufficient data, and three hostile
  inputs: a prompt-injection request, a fabricated "system override" directive and logs that contain secret-like
  markers. The hostile cases expect a safe `insufficient_data` answer.
- **Six bad reference answers that must be rejected.** A hang claimed from dropped logs, from ingestion delay or from
  silence alone, a claim that leans on another instance's evidence, a citation to a value that is not in the evidence,
  and confidence the evidence does not support.

Other tests in `tests/` cover the surrounding controls, such as forged tool scope, redaction and
erasure. The case tests are in `tests/test_security_controls.py`.

## Run the offline evaluation

It makes no network or AWS calls and costs nothing. CI runs it.

```bash
python scripts/dev/evaluate_diagnostics.py --output .build/diagnostics-evaluation.json
```

For each case it validates the reference answer against that case's evidence. It also checks that the redaction step
removes the fixtures' secret-like and email markers. A case passes when the grade matches and those checks hold.
The required pass rate is 100%. Exit code 0 means pass, 1 means a case failed and 2 means an error. The output file
records `model: null` and `live_model_evaluation: NOT_RUN`.

## Optional paid model run

This sends the 10 `VALID` cases to a real model, 3 to 5 times each (30 to 50 runs). The model is on Amazon Bedrock by
default, or at a Model API provider if you set `MODEL_API` (see below). Run it only when you have approved the model
charges, and write to an owner-only directory that git ignores.

```bash
umask 077; mkdir -p .local/evaluations; chmod 700 .local/evaluations
python scripts/dev/evaluate_diagnostics.py --live-model MODEL_ID --region REGION --allow-paid-model --repeats 3 --token-budget 100000 --output .local/evaluations/model-review.json
```

| Flag | Meaning |
|---|---|
| `--live-model MODEL_ID` | Model to grade: a Bedrock model ID, or with `MODEL_API` set the model name sent to the provider. Needs `--region` and `--allow-paid-model`, or the script refuses and exits 2. |
| `--region REGION` | Bedrock region. The script still requires it when `MODEL_API` is set, but the model calls do not use it. |
| `--repeats N` | Runs per case, 3 to 5 (default 3). |
| `--token-budget N` | Cap on reserved tokens for the whole run, 2,048 to 100,000 (default 32,000). |

**What it calls.** With Bedrock, only `CountTokens` and `Converse` in the region you pass, with your default AWS
credentials. With `MODEL_API` set, no Bedrock calls: only HTTPS requests to the provider and one Secrets Manager
`GetSecretValue` for the pinned key version, using your default AWS credentials. If `MODEL_API` is already set in your
shell, a paid run uses it instead of Bedrock, so unset it for a Bedrock run. The tools read the fixture evidence and
reject any other instance. It never reads your CloudWatch, changes anything, sends email or calls deployed tools or
AgentCore.

**Limits.** A reservation is the counted input plus the output ceiling for each model step, and it is never refunded.
When the whole-run cap is used up, the remaining runs stop with `EVALUATION_BUDGET_EXHAUSTED` and count as failures.
Nothing is retried. Each run also uses Tracaus's default chat limits: up to 24,000 reserved tokens (less if your cap is
smaller), 6 model steps, 6 tool calls, 12 log queries, 1,024 output tokens per step and a 180-second deadline. A small
cap can run out before all runs finish, so check the failures before you approve another run. An AWS or provider error,
such as access denied or a rate limit, ends the whole run with exit 2 and saves no results, though calls already made
are billed. The script
does not promise a dollar amount. Prices, model access and quotas are your decisions.

**Grading.** A run passes when the checked diagnosis is `VALID` and contains the case's expected finding kind. The
overall status is `PASS_PROVISIONAL` at a 90% pass rate and `FAIL` below it. The 90% bar and 3 repeats are proposals
that your reviewer must approve, and the output says `customer_threshold_approval: PENDING`. The file keeps only
checked diagnoses and their sources. Rejected drafts are not saved. Have a named person read them for alternative
causes, evidence quality, advice and withheld claims, not just the pass rate. Any cross-scope access, secret leak or
unauthorized side effect fails security acceptance, whatever the pass rate.

## Run against a Model API provider

Use this to qualify a non-Bedrock model before you rely on it. It is paid and has never been run against a live
provider by the maintainers.

1. Create the API key secret in AWS Secrets Manager and note its full ARN and its exact version ID. Your AWS
   credentials need `secretsmanager:GetSecretValue` on that version. The key never goes in a file or in the command.
2. Export `MODEL_API` in the shell that runs the script. The script does not read `.env`. The value is JSON with the
   keys `protocol` (`openai` or `anthropic`), `base_url`, `secret_arn`, `secret_version` and, for `openai` only, an
   optional `bytes_per_token`. Use the values you plan to deploy.
3. Run the paid command above. `--live-model` is the provider's model name. The script still requires `--region`, so
   pass one.

```bash
# Shape only: put all required keys in the JSON, as in the list above.
export MODEL_API='{"protocol":"openai","base_url":"https://api.example.com/v1", ... }'
```

An invalid `MODEL_API` ends the run with exit 2 before any model call. Caveats:

- **Token accounting is not qualified.** An OpenAI-compatible provider has no token-count call, so Tracaus reserves a
  local estimate (UTF-8 bytes divided by `bytes_per_token`, plus 64). It is not an upper bound, so the whole-run token
  cap is only as good as that estimate. If the provider reports more input than reserved, or no usage, that run stops
  with `TOKEN_ACCOUNTING_MISMATCH` and counts as a failure. The stop comes after the call, which is already billed. The
  Anthropic protocol uses the provider's own count endpoint, which Anthropic documents as an estimate.
- **No retries.** A call is never retried, and one that takes longer than 24 seconds fails.
- **The result does not name the provider.** The output records the model and region you passed, not the provider or
  `base_url`. Keep that in your own private record.
- **Quality is unmeasured.** The maintainers have not measured diagnosis quality on non-Claude models. Read the
  results as described under Grading.

## Limits of synthetic evaluation

- The offline run grades reference answers, not what a model writes. Rejecting a bad answer is a useful check, but it
  does not show that any model is safe.
- Ten cases on one fictional instance cannot cover your services. Repeated runs cannot prove prompt injection is
  impossible or that the wording is always right.
- A real-data evaluation needs your approval of data classification and redaction, plus private fixtures. This suite
  does not provide them.
- It does not test a deployment. Live model, tool and AgentCore behavior is checked in
  [ACCEPTANCE.md](../../docs/ACCEPTANCE.md).

Keep your own results private.
