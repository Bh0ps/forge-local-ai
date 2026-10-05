# Windows signing setup

Signing is not configured for the current preview. Its installer is **unsigned**;
automatic installation remains disabled. A real certificate from a trusted code
signing provider is required. A self-signed certificate or GitHub attestation
does not remove Windows trust warnings or establish an approved publisher.

The requested publisher is an individual outside the US/Canada. Microsoft
Artifact Signing has been skipped. Its current individual public-trust
eligibility is US/Canada, so that route is not an assumed fit. The optional
[Microsoft eligibility documentation](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart)
is retained as reference; no Azure account or resources are created here.

## Individual certificate options

[Certum Open Source Code Signing on SimplySign](https://shop.certum.eu/open-source-code-signing-on-simplysign.html)
supports an individual open-source publisher and a cloud-backed certificate
usable through SimplySign Desktop. Review the
[required identity documents](https://support.certum.eu/en/code-signing-required-documents/)
and current availability before selecting it. Its shop showed out of stock at
the time of implementation; a catalog price is not a purchase commitment.

[SSL.com code signing](https://www.ssl.com/products/software-integrity/code-signing/)
and its [eSigner service](https://www.ssl.com/products/software-integrity/signing-service/)
provide an individual-validation alternative with separate certificate and
cloud-signing charges. Confirm country eligibility, certificate subject fields,
hardware/cloud requirements and renewal costs directly with the provider. No
provider purchase, account verification or credential collection is performed
by this project. These remain external prerequisites for a signed release.

A public certificate necessarily exposes its publisher identity and provider
certificate fields. That is distinct from Git source privacy: repository commits
retain the neutral Forge contributor identity, and contain no legal name, home
address, government documents, certificate account IDs, secrets or local paths.
Keep certificate settings and account verification outside the repository.

## Concrete manual signing sequence

1. Obtain and activate the trusted individual code-signing certificate privately.
   Install the Windows SDK SignTool and the provider's supported signing client.
   SimplySign Desktop must expose its certificate through the Windows certificate
   store before the included script can use it.
2. In a private shell, set `FORGE_PUBLISHER_SUBJECT` to the exact verified
   certificate subject and `FORGE_SIGNER_THUMBPRINT` to its certificate reference.
   Do not put these in a committed `.env` file or paste account secrets in chat.
3. Run `python scripts/release_build.py build --work <new-folder> --version 4.2.0`
   using the validated lock environment and compiled frontend. The publisher
   policy is generated only in the private build staging folder and embedded in
   the application before signing.
4. Use `scripts/release_sign.ps1 -Files <Forge.exe>,<ForgeBrowserHost.exe>
   -SignTool <signtool.exe>` to sign **both** application executables. The script
   requests SHA256 with an RFC3161 timestamp and verifies the Windows trust chain,
   publisher and timestamp. The provider may require an interactive approval.
5. Run the `installer` stage with `--require-signed` and the verified Inno compiler.
   Sign the resulting Setup executable, then run `package --require-signed`.
   Never modify an executable after signing it.
6. Create a draft at the exact source tag and upload Setup, Portable ZIP,
   `Forge-release.json` and `SHA256SUMS.txt`. Verify the same files on a clean PC.
7. Configure a GitHub **forge-release** environment with required reviewers and
   a private `FORGE_PUBLISHER_SUBJECT` reference. Run `signed-release.yml` at the
   exact tagged commit. The job checks the fixed repository/draft, expected
   assets, exact version, both application signatures, installer signature,
   timestamp, publisher, package hashes and safe archive paths before publishing.

The manual publication job has only the permissions needed to inspect and
publish that protected draft; it has no certificate private key, signing account
password or OIDC Azure role. Normal test/preview CI remains read-only. Future
cloud signing can use the selected provider's documented credential/OIDC model
after it is authorized; no placeholder cloud identity is treated as valid.

Publisher changes require an explicitly reviewed trust transition. Users cannot
replace the embedded publisher policy through model settings, plugins or a
release manifest. Until the first trusted installer is produced and manually
verified, the updater truthfully reports **signing setup required**.
