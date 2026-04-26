# Challenge — Itaú Gamification Platform

## Business Problem

Itaú Unibanco wanted to increase customer engagement with digital financial products — specifically the **PIX** instant payment system — by rewarding customers who use the platform actively.

The challenge was to design and implement a **gamification layer** on top of the existing banking transaction infrastructure. The solution must be:

- **Non-intrusive**: the transaction flow must not be affected by gamification processing
- **Scalable**: thousands of transactions per second must be supported without adding latency to the critical path
- **Resilient**: gamification failures must never cause transaction failures
- **Auditable**: every event must be traceable for compliance and debugging

---

## The Gamification Scenario

Every time a customer performs a **PIX** transaction via the Itaú mobile app, the system awards **points** to the customer based on the transaction amount. Accumulated points unlock **levels** and can be exchanged for **benefits**.

### Why Gamification?

Banks compete for engagement. PIX is free by regulation, so banks must differentiate through experience. Gamification creates positive feedback loops — customers who receive rewards are more likely to:

- Use PIX more frequently
- Recommend the app
- Engage with other products (credit cards, investments)

---

## Rules

### 1. Missions by Amount Range

Missions award points when a PIX transaction falls within a specific amount range. Each mission can be completed only once per customer.

| Mission       | Range (BRL)          | Points Awarded |
|---------------|----------------------|----------------|
| PIX Small     | R$ 0.01 – R$ 1,000   | 5 points        |
| PIX Medium    | R$ 1,000 – R$ 9,999  | 10 points       |
| PIX Large     | R$ 10,000+           | 20 points       |

- A customer can complete **multiple missions** with a single transaction if it falls within overlapping ranges.
- Once a mission is completed, it is **never awarded again** to the same customer (idempotent by design).
- Missions are configurable in the database and can be enabled/disabled without code changes.

### 2. Idempotency

The system must guarantee that the same transaction event is **never processed more than once**, even in the presence of:

- Network retries
- SQS redeliveries
- Application restarts

**Implementation:** A `ProcessedEvent` record is stored in the H2 relational database for each processed event ID. Additionally, a `GameEventDocument` is stored in MongoDB as an audit log. Both layers are checked before processing begins.

### 3. Monthly Reset

Customer points and level are **reset at the beginning of each calendar month**. This encourages continuous engagement rather than one-time participation.

**Reset logic:** On every event processing, the system compares the `lastReset` field of `CustomerProgress` with the current date. If they differ by month or year, points are set to 0 and level is reset to 1.

### 4. Levels

Levels represent the customer's status tier. They are recalculated after every transaction event based on the customer's accumulated points.

| Level | Minimum Points |
|-------|---------------|
| 1     | 0             |
| 2     | 100           |
| 3     | 500           |
| 4     | 1,000         |
| 5     | 2,000         |

Level rules are stored in the database and can be modified without code changes.

### 5. Benefit Redemption

Customers can redeem benefits (e.g., cashback, discounts) using their accumulated points. Redemptions are tracked per customer per month.

**Rule:** If a customer has already redeemed a benefit in the current month, further transactions are still counted but no additional redemption is triggered automatically.

The `BenefitRedemption` entity records each redemption event with the customer ID, amount, and timestamp.

---

## Out of Scope (for this challenge)

- Debit card / credit card gamification (only PIX is supported)
- Cross-channel campaigns (only MOBILE channel is accepted)
- Points expiration beyond monthly reset
- Customer-facing benefit redemption API (backend tracking only)
