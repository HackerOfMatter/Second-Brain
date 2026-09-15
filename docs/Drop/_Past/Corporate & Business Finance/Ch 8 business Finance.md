---
type: finance_cheatsheet
topic: Capital Budgeting
tags:
  - finance
  - investment
  - capital_budgeting
  - valuation
  - NPV
  - MIRR
  - payback
  - profitability_index
aliases:
  - Capital Budgeting
---
[[Payback Period Method]]
[[[Profitability Index (PI)]]
]

## 🧮 Related Ratios

|Metric|Formula|Decision Rule|Key Insight|
|---|---|---|---|
|**NPV**|PV(inflows) − PV(outflows)|Accept if > 0|Measures value creation|
|**PI**|PV(inflows)/Initial outlay|Accept if > 1|Benefit per $ invested|
|**Payback Period**|Years to recover cost|Lower = better|Measures liquidity|
|**IRR**|Discount rate where NPV = 0|Accept if > required return|Measures rate of return|
|**MIRR**|Modified rate with realistic reinvestment|Accept if > required return|Adjusted internal rate|

# 🧭 Capital Budgeting Cheat Sheet
> **Purpose:** Evaluate long-term investments and projects to determine which add the most value to the firm.

---

## 🧱 Core Concepts

### **Capital Budgeting**
- **Definition:** The process of planning and evaluating investments in long-term assets (projects, equipment, etc.).
- **Goal:** Maximize shareholder wealth by selecting projects with the highest value creation.
- **Inputs:** Estimated cash inflows, outflows, discount rate (required return), and project life.

---

## 💰 Net Present Value (NPV)

### **Concept**
- Measures the difference between the **present value of cash inflows** and **present value of cash outflows**.
- Accept projects with **NPV > 0** → adds value to the firm.

### **Formula**
\[
NPV = \sum_{t=0}^{n} \frac{CF_t}{(1+r)^t}
\]
where:
- \( CF_t \): Cash flow at time t  
- \( r \): Discount rate  
- \( n \): Project life


- [[Time Value of Money]]
    
- [[WACC – Weighted Average Cost of Capital]]
    
- [[Capital Rationing Decisions]]
    
- [[Internal Rate of Return (IRR)]]
    
- [[Incremental Cash Flow Analysis]]
### **In Excel**
```excel
=NPV(rate, value1, [value2,...]) + initial_investment

⚠️ Remember: Excel’s `NPV()` assumes the first cash flow occurs **one period in the future** — add the initial cost manually.

### **Advantages**

- Considers time value of money
    
- Directly measures value creation
    
- Uses all cash flows
    

### **Disadvantages**

- Requires accurate discount rate
    
- Can be harder to explain to non-financial users
  
  '''
  
  ;;;
