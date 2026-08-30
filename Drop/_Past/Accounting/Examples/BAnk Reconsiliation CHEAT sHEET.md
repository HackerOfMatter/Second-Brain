# 🧾 Bank Reconciliation Cheat Sheet

## 🔄 GENERAL RULES

| Type of Item                            | Add/Subtract | Affects Bank or Book? | Cash Entry in Books        |
| --------------------------------------- | ------------ | --------------------- | -------------------------- |
| **Deposits in transit**                 | Add          | Bank                  | None (already in books)    |
| **Outstanding checks**                  | Subtract     | Bank                  | None (already in books)    |
| **Bank service charges**                | Subtract     | Books                 | Credit                     |
| **NSF checks** (customer bounced check) | Subtract     | Books                 | Credit                     |
| **Interest earned**                     | Add          | Books                 | Debit                      |
| **Note collected by bank**              | Add          | Books                 | Debit                      |
| **Bank errors (overcharges)**           | Add          | Bank                  | None (wait for correction) |
| **Bank errors (undercharges)**          | Subtract     | Bank                  | None (wait for correction) |
| **Company error (forgot check)**        | Subtract     | Books                 | Credit                     |
| **Company error (understated deposit)** | Add          | Books                 | Debit                      |

---

## 💡 HOW TO KNOW WHAT TO DO

### 📘 When to Adjust the **Bank Balance**
- Use for **timing differences**:
  - Deposits in transit ✅ Add
  - Outstanding checks ✅ Subtract
  - Bank errors ✅ Adjust as needed

### 🧾 When to Adjust the **Book Balance (Your Ledger)**
- Use for **items the company hasn’t recorded yet**:
  - NSF check from customer ✅ Credit Cash
  - Bank service charges ✅ Credit Cash
  - Interest earned ✅ Debit Cash
  - Note collected by bank ✅ Debit Cash

### 🧠 When to **Ignore (Not Shown on Reconciliation)**
- Already recorded in books and cleared
- Postdated transactions
- Night deposits after bank closes
- Errors unrelated to your account

---

## 📌 Cash Account Rules (Book Adjustments)

| Situation                    | Cash Account Entry |
|-----------------------------|--------------------|
| Increase in bank balance    | **Debit** Cash     |
| Decrease in bank balance    | **Credit** Cash    |

---

## 🧠 Mnemonic:
> **D-C: Debit = Cash In, Credit = Cash Out**

---

## ✅ Example Summary:

| Item                             | Action        | Book Entry  |
|----------------------------------|---------------|-------------|
| Bank collected note for you      | Add to books  | Debit Cash  |
| Bank charged monthly fee         | Subtract book | Credit Cash |
| Your check bounced (NSF)         | Subtract book | Credit Cash |
| You forgot to record a check     | Subtract book | Credit Cash |
| Deposit in transit               | Add to bank   | —           |
| Outstanding check                | Subtract bank | —           |

