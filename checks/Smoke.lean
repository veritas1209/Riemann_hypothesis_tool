import Statements

theorem setup_square_nonnegative (x : ℝ) : 0 ≤ x ^ 2 := by
  positivity

#check RiemannHypothesis
#check DirichletLZerosOnLine
#print axioms setup_square_nonnegative
