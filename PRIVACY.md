# FaultMaven Slack App — Privacy Policy

*Effective Date: June 2, 2025*
*Last Updated: July 18, 2026*

Welcome to FaultMaven ("we," "us," "our," or "FaultMaven"). We are committed to
protecting your privacy and handling your information in an open and transparent
manner. This Privacy Policy explains how we collect, use, disclose, and safeguard
your information when you use the FaultMaven Slack application and visit our
website, www.faultmaven.ai (the "Site"), and engage with any services we may
offer (collectively, the "Services").

Please read this Privacy Policy carefully. If you do not agree with the terms of
this Privacy Policy, please do not access the Services.

## 1. Information We Collect

Our data collection is focused on:

- **Information You Provide Voluntarily:** Such as your email address when you
  sign up for our waitlist or newsletter, and any information (name, contact
  details, inquiry details) you provide when you contact us.

- **Slack Workspace Data:** When you install FaultMaven into your Slack
  workspace, we receive your workspace identifier and a bot token that allows us
  to respond to messages where FaultMaven is explicitly invoked. We do not read
  or store ambient channel messages.

- **Investigation Data:** Messages, files, and context you explicitly share with
  FaultMaven during an investigation are forwarded to the FaultMaven backend for
  processing. Evidence is processed through our PII redaction pipeline
  (Presidio).

- **Standard Website Usage Data:** Like most websites, we may use cookies and
  similar tracking technologies to collect non-personally identifiable
  information about your interaction with our Site.

## 2. How We Use Your Information

- To provide and operate the FaultMaven troubleshooting service within your Slack
  workspace.
- To communicate with you about FaultMaven, including development progress and
  responses to your inquiries.
- To gather feedback to inform and shape FaultMaven's development.
- To analyze usage to improve our services.

We will not sell your personal information to third parties.

## 3. Data Security

We are implementing reasonable administrative, technical, and physical security
measures to help protect your information. While we strive to use commercially
acceptable means to protect your personal information, no method of transmission
over the Internet or method of electronic storage is 100% secure.

## 4. Data Handling & Slack-Specific Practices

FaultMaven operates on a **summon-only** model: it acts only when explicitly
invoked via @mention, the Ask shortcut, or a direct message. It does not
passively read or store channel conversations. Investigation data is processed
in-memory and forwarded to the FaultMaven backend; the Slack agent does not
persist evidence content itself. Any investigation data securely forwarded to
upstream LLM providers utilizes enterprise-grade Zero-Data-Retention (ZDR) APIs,
ensuring data is processed statelessly and never retained for foundational model
training.

## 5. Future Policy Updates

This Privacy Policy may be updated as the FaultMaven product evolves. A more
detailed policy addressing data handling related to the FaultMaven AI Copilot
product will be made available as needed.

## 6. Your Choices & Rights

You may opt-out of any future email communications by following the unsubscribe
link or by contacting us directly at engineering@faultmaven.ai. You can uninstall
FaultMaven from your Slack workspace at any time via your Slack workspace
settings. Depending on your jurisdiction, you may have other rights regarding
your personal data.

## 7. Changes to This Privacy Policy

We reserve the right to make changes to this Privacy Policy at any time and for
any reason. We will alert you about any changes by updating the "Last Updated"
date of this Privacy Policy.

## 8. Contact Us

If you have questions or comments about this Privacy Policy, please contact us
at:

FaultMaven
engineering@faultmaven.ai
