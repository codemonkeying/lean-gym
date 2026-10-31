def parse(s):
    """
    Parse a string input and return its integer value.
    
    Args:
        s (str): The string to parse
        
    Returns:
        int: The parsed integer
        
    Raises:
        TypeError: If input is not a string
        ValueError: If string cannot be converted to integer or is empty
    """
    # Input validation
    if not isinstance(s, str):
        raise TypeError("Input must be a string")
    
    # Check for empty string
    if len(s) == 0:
        raise ValueError("Input string cannot be empty")
        
    # Check for whitespace-only strings
    if s.isspace():
        raise ValueError("Input string cannot contain only whitespace")
        
    # Parse the string to integer
    return int(s)