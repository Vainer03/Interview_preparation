package cracking_the_coding_interview.arrays_and_strings;

public class task1_1_1_9 {
    
    // task 1.1
    
    boolean isUniqueChars(String str) {
       int checker = 0;
       for (int i = 0; i < str.length(); i++) {
        int val = str.charAt(i) - 'a';
        if ((checker & (1 << val)) > 0) {
            return false;
        }
        checker |= (1<<val);
       } 
       return true;
    }
    
    // task 1.2

    String sort(String s) {
        char[] content = s.toCharArray();
        java.util.Arrays.sort(content);
        return new String(content);
    }

    boolean permutation_1(String s, String t) {
        if (s.length() != t.length()) {
            return false;
        }
        return sort(s).equals(sort(t));
    }

    boolean permutation_2(String s, String t) {
        if (s.length() != t.length()) {
            return false;
        }

        int[] letters = new int[128];
        char[] s_array = s.toCharArray();
        for (char c : s_array) {
            letters[c]++;
        }

        for (int i = 0; i < t.length(); i++) {
            int c = (int) t.charAt(i);
            letters[c]--;
            if (letters[c] < 0) {
                return false;
            }
        }
        return true;
    }

    // task 1.3

    // void replaceSpaces(char[] str, int length) {
    //     int spaceCount = 0, newLength, i; 
    // }





}
